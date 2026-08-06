"""
Emit events from JSONL files to the Store Intelligence API.
Reads events in actual challenge format and sends in batches.
"""
import json
import requests
import argparse
import sys
import time


def emit_events(input_path: str, api_url: str = "http://localhost:8000", batch_size: int = 100):
    print(f"📤 Emitting events from {input_path} to {api_url}")

    events = []
    with open(input_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"  ⚠️ Skipping malformed line: {e}")

    print(f"   Loaded {len(events)} events")

    total_accepted = 0
    total_duplicates = 0
    total_rejected = 0

    for i in range(0, len(events), batch_size):
        batch = events[i:i + batch_size]
        try:
            resp = requests.post(
                f"{api_url}/events/ingest",
                json={"events": batch},
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                accepted = data.get("accepted", 0)
                dups = data.get("duplicates_ignored", 0)
                rejected = len(data.get("rejected", []))
                total_accepted += accepted
                total_duplicates += dups
                total_rejected += rejected
                print(f"   Batch {i // batch_size + 1}: accepted={accepted} dups={dups} rejected={rejected}")
            else:
                print(f"   ❌ Batch {i // batch_size + 1}: HTTP {resp.status_code} — {resp.text[:200]}")
        except requests.exceptions.ConnectionError:
            print(f"   ❌ API not reachable at {api_url}")
            sys.exit(1)
        except Exception as e:
            print(f"   ❌ Error: {e}")

        time.sleep(0.1)

    print(f"\n✅ Done: accepted={total_accepted} duplicates={total_duplicates} rejected={total_rejected}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Emit events to API")
    parser.add_argument("--input", required=True, help="Path to JSONL file")
    parser.add_argument("--api_url", default="http://localhost:8000", help="API base URL")
    parser.add_argument("--batch_size", type=int, default=100, help="Events per batch")
    args = parser.parse_args()
    emit_events(args.input, args.api_url, args.batch_size)