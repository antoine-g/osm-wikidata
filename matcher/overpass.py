#!/usr/bin/python3
import re
import requests
import os.path
import json
import simplejson
from .mail import error_mail
from flask import current_app
from time import sleep
from . import user_agent_headers
from collections import defaultdict

re_slot_available = re.compile('^Slot available after: ([^,]+), in (\d+) seconds?\.$')
re_available_now = re.compile('^\d+ slots available now.$')

name_only_tag = {'area=yes', 'type=tunnel', 'leisure=park', 'leisure=garden',
        'site=aerodome', 'amenity=hospital', 'boundary', 'amenity=pub',
        'amenity=cinema', 'ruins', 'retail=retail_park',
        'amenity=concert_hall', 'amenity=theatre', 'designation=civil_parish'}

name_only_key = ['place', 'landuse', 'admin_level', 'water', 'man_made',
        'railway', 'aeroway', 'bridge', 'natural']

overpass_url = 'http://overpass-api.de/api/interpreter'
# overpass_url = 'http://overpass.osm.rambler.ru/cgi/interpreter'
# overpass_url = 'http://api.openstreetmap.fr/oapi/interpreter'

class RateLimited(Exception):
    pass

class Timeout(Exception):
    pass

def oql_for_area(overpass_type, osm_id, tags, bbox, buildings):
    union = []

    for key, values in group_tags(tags).items():
        u = oql_element_filter(key, values)
        if u:
            union += u

    offset = {'way': 2400000000, 'rel': 3600000000}
    area_id = offset[overpass_type] + int(osm_id)

    if buildings:
        oql_building = '''
    node(area.a)["building"][~"^(addr:housenumber|.*name.*)$"~"{}",i];
    way(area.a)["building"][~"^(addr:housenumber|.*name.*)$"~"{}",i];
    rel(area.a)["building"][~"^(addr:housenumber|.*name.*)$"~"{}",i];
'''.strip().format(buildings, buildings, buildings)
    else:
        oql_building = ''

    oql_template = '''
[timeout:300][out:xml][bbox:{}];
area({}) -> .a;
(
{}
) -> .b;
(
    {}({});
    node.b[~"^(addr:housenumber|.*name.*)$"~".",i];
    way.b[~"^(addr:housenumber|.*name.*)$"~".",i];
    rel.b[~"^(addr:housenumber|.*name.*)$"~".",i];
    {}
);
(._;>;);
out;'''
    return oql_template.format(bbox, area_id, '\n'.join(union), overpass_type, osm_id, oql_building)

def group_tags(tags):
    '''given a list of keys and tags return a dict group by key'''
    ret = defaultdict(list)
    for tag_or_key in tags:
        if '=' in tag_or_key:
            key, _, value = tag_or_key.partition('=')
            ret[key].append(value)
        else:
            ret[tag_or_key] = []
    return dict(ret)

def oql_element_filter(key, values, filters='area.a'):
    # optimisation: we only expect route, type or site on relations
    relation_only = key in {'site', 'type', 'route'}

    if values:
        if len(values) == 1:
            tag = '"{}"="{}"'.format(key, values[0])
        else:
            tag = '"{}"~"^({})$"'.format(key, '|'.join(values))
    else:
        tag = '"{}"'.format(key)

    return ['{}({})[{}];'.format(t, filters, tag)
            for t in (('rel',) if relation_only else ('node', 'way', 'rel'))]

def oql_from_tag(tag, large_area, filters='area.a'):
    if tag == 'highway':
        return []
    # optimisation: we only expect route, type or site on relations
    relation_only = tag == 'site'
    if large_area or tag in name_only_tag or any(tag.startswith(k) for k in name_only_key):
        name_filter = '[name]'
    else:
        name_filter = '[~"^(addr:housenumber|.*name.*)$"~".",i]'
    if '=' in tag:
        k, _, v = tag.partition('=')
        if tag == 'type=waterway' or k == 'route' or tag == 'type=route':
            return []  # ignore because osm2pgsql only does multipolygons
        if k in {'site', 'type', 'route'}:
            relation_only = True
        if not k.isalnum() or not v.isalnum():
            tag = '"{}"="{}"'.format(k, v)
    elif not tag.isalnum():
        tag = '"{}"'.format(tag)

    return ['\n    {}({})[{}]{};'.format(t, filters, tag, name_filter)
            for t in (('rel',) if relation_only else ('node', 'way', 'rel'))]

    # return ['\n    {}(area.a)[{}]{};'.format(t, tag, name_filter) for ('node', 'way', 'rel')]

def oql_from_wikidata_tag_or_key(tag_or_key, filters):
    osm_type, _, tag = tag_or_key.partition(':')
    osm_type = osm_type.lower()
    if not {'key': False, 'tag': True}[osm_type] == ('=' in tag):
        return []

    relation_only = tag == 'site'

    if tag in name_only_tag or any(tag.startswith(k) for k in name_only_key):
        name_filter = '[name]'
    else:
        name_filter = '[~"^(addr:housenumber|.*name.*)$"~".",i]'
    if osm_type == 'tag':
        k, _, v = tag.partition('=')
        if k in {'site', 'type', 'route'}:
            relation_only = True
        if not k.isalnum() or not v.isalnum():
            tag = '"{}"="{}"'.format(k, v)
    elif not tag.isalnum():
        tag = '"{}"'.format(tag)

    return ['\n    {}({})[{}]{};'.format(t, filters, tag, name_filter)
            for t in (('rel',) if relation_only else ('node', 'way', 'rel'))]

def parse_status(status):
    lines = status.splitlines()
    limit = 'Rate limit: '

    assert lines[0].startswith('Connected as: ')
    assert lines[1].startswith('Current time: ')
    assert lines[2].startswith(limit)

    slots = []
    for i in range(3, len(lines)):
        line = lines[i]
        if not line.startswith('Slot available after:'):
            break
        m = re_slot_available.match(line)
        slots.append(int(m.group(2)))

    next_line = lines[i]
    assert (re_available_now.match(next_line) or
            next_line == 'Currently running queries (pid, space limit, time limit, start time):')

    return {
        'rate_limit': int(lines[2][len(limit):]),
        'slots': slots,
        'running': len(lines) - (i + 1)
    }

def get_status(url=None):
    if url is None:
        url = 'https://overpass-api.de/api/status'
    status = requests.get(url).text
    return parse_status(status)

def wait_for_slot(status=None, url=None):
    if status is None:
        status = get_status(url=url)
    slots = status['slots']
    if slots:
        print('waiting {} seconds'.format(slots[0]))
        sleep(slots[0] + 1)

def item_filename(wikidata_id, radius):
    assert wikidata_id[0] == 'Q'
    overpass_dir = current_app.config['OVERPASS_DIR']
    return os.path.join(overpass_dir, '{}_{}.json'.format(wikidata_id, radius))

def existing_item_filename(wikidata_id):
    assert wikidata_id[0] == 'Q'
    overpass_dir = current_app.config['OVERPASS_DIR']
    return os.path.join(overpass_dir, '{}_existing.json'.format(wikidata_id))

def item_query(oql, wikidata_id, radius=1000, refresh=False):
    filename = item_filename(wikidata_id, radius)

    if not refresh and os.path.exists(filename):
        return json.load(open(filename))['elements']

    r = requests.post(overpass_url, data=oql, headers=user_agent_headers())

    if r.status_code == 429 and 'rate_limited' in r.text:
        error_mail('item query: overpass rate limit', oql, r)
        raise RateLimited

    if len(r.content) < 2000 and b'<title>504 Gateway' in r.content:
        error_mail('item query: overpass 504 gateway timeout', oql, r)
        raise Timeout

    try:
        data = r.json()
    except simplejson.scanner.JSONDecodeError:
        error_mail('item overpass query error', oql, r)
        raise

    json.dump(data, open(filename, 'w'))
    return data['elements']

def get_existing(wikidata_id, refresh=False):
    filename = existing_item_filename(wikidata_id)

    if not refresh and os.path.exists(filename):
        return json.load(open(filename))['elements']

    oql = '''
[timeout:300][out:json];
(node[wikidata={qid}]; way[wikidata={qid}]; rel[wikidata={qid}];);
out qt center tags;
'''.format(qid=wikidata_id)

    r = requests.post(overpass_url, data=oql, headers=user_agent_headers())

    if r.status_code == 429 and 'rate_limited' in r.text:
        error_mail('get existing: overpass rate limit', oql, r)
        raise RateLimited

    if len(r.content) < 2000 and b'<title>504 Gateway' in r.content:
        error_mail('item query: overpass 504 gateway timeout', oql, r)
        raise Timeout

    try:
        data = r.json()
    except simplejson.scanner.JSONDecodeError:
        error_mail('item overpass query error', oql, r)
        raise

    json.dump(data, open(filename, 'w'))
    return data['elements']

def get_tags(elements):
    union = {'{}({});\n'.format({'relation': 'rel'}.get(i.osm_type, i.osm_type), i.osm_id)
             for i in elements}

    oql = '''
[timeout:300][out:json];
({});
out qt tags;
'''.format(''.join(union))

    r = requests.post(overpass_url, data=oql, headers=user_agent_headers())

    return r.json()['elements']

def items_as_xml(items):
    assert items
    union = ''
    for item, osm in items:
        union += '{}({});\n'.format(osm.osm_type, osm.osm_id)

    oql = '({});(._;>);out meta;'.format(union)

    r = requests.post(overpass_url,
                      data=oql,
                      headers=user_agent_headers())

    if r.status_code == 429 and 'rate_limited' in r.text:
        error_mail('items_as_xml: overpass rate limit', oql, r)
        raise RateLimited

    return r.content
