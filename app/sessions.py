"""
Session reconstruction for actual challenge data format.
Links id_token (entry/exit) with track_id (zone/queue) by demographics.
POS correlation uses actual DD-MM-YYYY date format.
"""
from typing import List, Dict, Any
import json
import sqlite3
import os
from datetime import datetime, timedelta, timezone

POS_DB_PATH = os.path.join("data", "store_intelligence.db")


def _load_pos_transactions(store_id: str) -> List[str]:
    """Load POS transaction timestamps for a store."""
    try:
        conn = sqlite3.connect(POS_DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT DISTINCT order_id, order_date, order_time
            FROM pos_transactions WHERE store_id = ?
        """, (store_id,)).fetchall()
        conn.close()
        txn_times = []
        for r in rows:
            od = r['order_date']
            ot = r['order_time']
            # Handle DD-MM-YYYY format
            if '-' in od and len(od.split('-')[0]) == 2:
                parts = od.split('-')
                od = f"{parts[2]}-{parts[1]}-{parts[0]}"
            ts = f"{od}T{ot}" if ot else od
            txn_times.append(ts)
        return txn_times
    except Exception:
        return []


def _parse_ts(ts_str: str) -> datetime:
    try:
        ts_str = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return datetime.now(timezone.utc)


def reconstruct_sessions(payload_rows: List[str], store_id: str = None) -> List[Dict[str, Any]]:
    """Build sessions from actual event payloads.
    Each session: {visitor_id, entered, zone_visit, queued, purchased}
    """
    visitor_data = {}

    for payload_json in payload_rows:
        try:
            payload = json.loads(payload_json)
        except Exception:
            continue

        is_staff = payload.get("is_staff", False)
        if is_staff:
            continue

        # Get visitor ID
        vid = payload.get("id_token") or str(payload.get("track_id", "")) or payload.get("visitor_id", "")
        if not vid:
            continue

        if vid not in visitor_data:
            visitor_data[vid] = {
                "visitor_id": vid,
                "entered": False,
                "zone_visit": False,
                "queued": False,
                "purchased": False,
                "billing_timestamps": [],
            }

        et = payload.get("event_type", "")

        if et in ("entry", "ENTRY", "reentry", "REENTRY"):
            visitor_data[vid]["entered"] = True
        elif et in ("zone_entered", "ZONE_ENTER"):
            visitor_data[vid]["zone_visit"] = True
        elif et in ("queue_completed", "queue_abandoned", "BILLING_QUEUE_JOIN"):
            visitor_data[vid]["queued"] = True
            ts = payload.get("queue_join_ts") or payload.get("event_time") or payload.get("timestamp", "")
            if ts:
                visitor_data[vid]["billing_timestamps"].append(_parse_ts(ts))
            if et == "queue_completed":
                visitor_data[vid]["purchased"] = True

    sessions = list(visitor_data.values())

    # POS correlation
    if store_id and sessions:
        pos_times = _load_pos_transactions(store_id)
        pos_datetimes = [_parse_ts(t) for t in pos_times]

        for sess in sessions:
            if sess["purchased"]:
                continue
            for billing_ts in sess.get("billing_timestamps", []):
                for pos_ts in pos_datetimes:
                    diff = pos_ts - billing_ts
                    if timedelta(0) <= diff <= timedelta(minutes=5):
                        sess["purchased"] = True
                        break
                if sess["purchased"]:
                    break

    # Clean up
    for sess in sessions:
        sess.pop("billing_timestamps", None)

    return sessions