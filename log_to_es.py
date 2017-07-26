#!/usr/bin/python -u
from __future__ import print_function
import fileinput
import sys
import os

from elasticsearch import Elasticsearch
from elasticsearch import helpers

INDEX_NAME = 'logteeworlds'

LEAVE_GAME_KILL_ID = "-3"

WEAPON_ID_2_NAME = {
    '5': 'ninja',
    '4': 'laser_rifle',
    '3': 'grenade',
    '2': 'shotgun',
    '1': 'normal_gun',
    '0': 'hammer',
    # for example you fell down the map
    # or walked in to lava
    '-1': 'the_map_killed_you',
    # you leave the game or switched team
    '-3': 'leave_team',
    # using the console command
    '-2': 'console_kill',
}

def extract_leave_event(log, timestamp):
    # looks like this:
    # leave player='0:nameless tee'

    player_part, _ = log[len("leave player='"):].split("'", 1)

    # the player part is `id:name`
    player_id, player_name = player_part.split(':', 1)

    return {
        "_index": INDEX_NAME,
        "_type": 'leave',
        'timestamp': timestamp*1000,
        'player_id': player_id,
        'player_name': player_name,
    }


def extract_team_join_event(log, timestamp):
    # looks like this:
    # team_join player='0:nameless tee' team=0

    # and the part about what has been picked
    player_part, team_part = log[len("team_join player='"):].split("' team=", 1)

    # the player part is `id:name`
    player_id, player_name = player_part.split(':', 1)

    return {
        "_index": INDEX_NAME,
        "_type": 'team_join',
        'timestamp': timestamp*1000,
        'team_id': team_part,
        'player_id': player_id,
        'player_name': player_name,
    }

def extract_velocity_event(log, timestamp):
    # looks like this:
    # velocity player='0:nameless tee' value=0.001953
    # Note: this log does not exists in the vanilla version

    # and the part about what has been picked
    player_part, velocity_part = log[len("velocity player='"):].split("' value=", 1)

    # the player part is `id:name`
    player_id, player_name = player_part.split(':', 1)

    return {
        "_index": INDEX_NAME,
        "_type": 'velocity',
        'timestamp': timestamp*1000,
        'player_id': player_id,
        'player_name': player_name,
        'velocity': float(velocity_part),
    }



def extract_pickup_event(log, timestamp):
    # looks like this:
    # pickup player='4:Lior  nco' item=1/0

    # we remove the part pickup player='
    # which give us the part about the player who picked up something
    # and the part about what has been picked
    player_part, item_part = log[len("pickup player='"):].split("' item=", 1)

    # the player part is `id:name`:w
    player_id, player_name = player_part.split(':', 1)

    # checked in /src/game/generated/protocol.h for the value
    # and /src/game/server/entities/pickup.cpp for the format
    item_pickup_to_name = {
        '0/0' : "health",
        '1/0' : "armor",
        '2/0' : "hammer", # should not be possible...
        '2/1' : "normal_gun", # should not be possible...
        '2/2' : "shotgun",
        '2/3' : "grenade",
        '2/4' : "laser_rifle",
        '3/5' : "ninja",
    }

    return {
        "_index": INDEX_NAME,
        "_type": 'pickup',
        'timestamp': timestamp*1000,
        'player_id': player_id,
        'player_name': player_name,
        # we get a "human name" and display the "log name"
        # as a fallback
        'item': item_pickup_to_name.get(item_part, item_part),
    }

def extract_kill_event(log, timestamp):
    #kill killer='1:Alex' victim='1:Alex' weapon=-3 special=0
    killer_part, other_part = log[len("kill player='"):].split("' victim='", 1)

    killer_id, killer_name = killer_part.split(':', 1)

    victim_part, other_part = other_part.split("' weapon=")
    victim_id, victim_name = victim_part.split(':', 1)

    weapon_part, special_part = other_part.split(" special=")

    is_suicide = (
        killer_id == victim_id and
        weapon_part != LEAVE_GAME_KILL_ID
    )

    return {
        "_index": INDEX_NAME,
        "_type": 'kill',
        'timestamp': timestamp*1000,
        'killer_id': killer_id,
        'killer_name': killer_name,
        'victim_id': victim_id,
        'victim_name': victim_name,
        'weapon': WEAPON_ID_2_NAME.get(weapon_part, weapon_part),
        'special': special_part,
        'is_suicide': is_suicide,
    }

def create_mapping(es_client):
    es_client.indices.put_mapping(
        index=INDEX_NAME,
        doc_type="kill",
        body={
            "properties": {
                "killer_id": {"type": "keyword"},
                "killer_name": {"type": "keyword"},
                "victim_id": {"type": "keyword"},
                "victim_name": {"type": "keyword"},
                "weapon": {"type": "keyword"},
                "special": {"type": "keyword"},
                "is_suicide": {"type": "boolean"},
                "timestamp": {"type": "date"},
            }
        }
    )
    es_client.indices.put_mapping(
        index=INDEX_NAME,
        doc_type="pickup",
        body={
            "properties": {
                "item": {"type": "keyword"},
                "player_id": {"type": "keyword"},
                "player_name": {"type": "keyword"},
                "timestamp": {"type": "date"},
            }
        }
    )

    es_client.indices.put_mapping(
        index=INDEX_NAME,
        doc_type="velocity",
        body={
            "properties": {
                "velocity": {"type": "float"},
                "player_id": {"type": "keyword"},
                "player_name": {"type": "keyword"},
                "timestamp": {"type": "date"},
            }
        }
    )

def main():
    es_ip = os.getenv('ELASTIC_SEARCH_IP')
    if es_ip is None:
        print("please set ELASTIC_SEARCH_IP env variable")
        return

    es_client = Elasticsearch(hosts=[es_ip])
    if not es_client.indices.exists(INDEX_NAME):
        es_client.indices.create(index=INDEX_NAME)
    create_mapping(es_client)

    events = []

    type_to_extractor = {
        'pickup': extract_pickup_event,
        'kill': extract_kill_event,
        'team_join': extract_team_join_event,
        'leave': extract_leave_event,
        'velocity': extract_velocity_event,
    }

    while True:
        line = sys.stdin.readline()
        if line is None:
            break
        line = line.strip()
        timestamp = _extract_timestamp(line)
        log_type = _extract_log_type(line)

        # for the moment we ignore
        # all the non in-game logs
        if log_type != "game":
            print("log_type not supported:", line)
            continue
        # extract the part after timestamp
        log = line.split(': ', 1)[1]

        event_type = _extract_event_type(log)

        extractor = type_to_extractor.get(event_type, None)

        if extractor is None:
            print("no extractor for type", event_type)
            print("original log:", log)
            continue

        event = extractor(log, timestamp)
        events.append(event)

        if len(events) > 5:
            helpers.bulk(es_client, events)
            print("*** send to ES****")
            events = []


def _extract_log_type(line):
    # log type is the second information between brackets
    log_type = line.split('[', 2)[2].split(']')[0]
    return log_type

def _extract_timestamp(line):
    # the timestamp is the first value between
    # brackets of the log line
    hexa_timestamp = line.split('[', 1)[1].split(']')[0]
    # the timestamp is in base16
    timestamp = int(hexa_timestamp, base=16)
    return timestamp

def _extract_event_type(log):
    return log.split(' ', 1)[0]

if __name__ == "__main__":
    main()
