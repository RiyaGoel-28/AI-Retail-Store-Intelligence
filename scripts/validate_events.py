#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
from app.schemas import Event


def load_events(path: Path):
    raw = path.read_text()
    try:
        data = json.loads(raw)
        # either list or object with events key
        if isinstance(data, dict) and 'events' in data:
            return data['events']
        if isinstance(data, list):
            return data
    except Exception:
        # try JSONL
        events = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            events.append(json.loads(line))
        return events
    return []


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--file', type=Path, required=True)
    args = p.parse_args()

    events = load_events(args.file)
    if not events:
        print('No events found in', args.file)
        raise SystemExit(2)

    ids = set()
    errors = 0
    for i, e in enumerate(events):
        try:
            ev = Event.parse_obj(e)
        except Exception as ex:
            print(f'Event index {i} failed validation:', ex)
            errors += 1
            continue
        if ev.event_id in ids:
            print(f'Duplicate event_id at index {i}: {ev.event_id}')
            errors += 1
        ids.add(ev.event_id)

    if errors:
        print(f'Validation completed with {errors} errors')
        raise SystemExit(3)
    print('All events validated successfully')


if __name__ == '__main__':
    main()
