"""
# PROMPT: Rewrite heatmap endpoint for actual zone_entered/zone_exited events with dwell computation
# CHANGES MADE: Compute dwell from zone_entered→zone_exited pairs, use zone_name for display,
# fixed data_confidence naming, staff exclusion.
# STAFF FIX: Set-based staff filter — visitor flagged on any event is excluded.
# DWELL FIX (Bug #4): avg_dwell now divides by the number of MATCHED enter/exit pairs,
# not the total number of enters. Unpaired enters were dragging the average to ~0.
# Also falls back to a visitor's `exit` event timestamp to close any zones still open at
# the end of their session, so long-dwell visitors who never zone-switched get counted.
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
import sqlite3
import os
from datetime import datetime

router = APIRouter()
DB_PATH = os.path.join("data", "events.db")


def _connect():
    try:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        raise HTTPException(status_code=503, detail="db_unavailable")


def _parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


@router.get("/stores/{store_id}/heatmap")
def store_heatmap(store_id: str):
    conn = _connect()

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT event_type, visitor_id, zone_id, zone_name, is_staff, timestamp, payload
            FROM events WHERE store_id = ?
            ORDER BY timestamp
        """, (store_id,))
        rows = cur.fetchall()

        # ── Set-based staff filter ──
        staff_visitor_ids = {
            row["visitor_id"] for row in rows
            if row["is_staff"] and row["visitor_id"]
        }

        zone_counts = {}        # zone → number of zone_entered events
        zone_dwell_ms = {}      # zone → sum of dwell ms across paired enters/exits
        zone_pairs = {}         # zone → number of successful (enter,exit) pairs
        zone_enter_times = {}   # (vid,zone) → enter timestamp (open pair)
        last_event_ts = {}      # vid → most recent timestamp seen (for end-of-session fallback)
        sessions = set()

        for row in rows:
            vid = row["visitor_id"]
            if not vid or vid in staff_visitor_ids:
                continue

            sessions.add(vid)
            ts = row["timestamp"]
            if ts:
                last_event_ts[vid] = ts

            et = row["event_type"]
            zone = row["zone_name"] or row["zone_id"] or ""

            if not zone:
                continue

            if et in ("zone_entered", "ZONE_ENTER"):
                zone_counts[zone] = zone_counts.get(zone, 0) + 1
                # If a previous enter for the same (vid, zone) is still open
                # (no exit recorded), close it against this enter as a fallback.
                # Otherwise the older one would just leak.
                key = (vid, zone)
                if key in zone_enter_times:
                    enter_dt = _parse_ts(zone_enter_times[key])
                    new_dt   = _parse_ts(ts)
                    if enter_dt and new_dt:
                        delta = int((new_dt - enter_dt).total_seconds() * 1000)
                        if delta > 0:
                            zone_dwell_ms[zone] = zone_dwell_ms.get(zone, 0) + delta
                            zone_pairs[zone]    = zone_pairs.get(zone, 0) + 1
                zone_enter_times[key] = ts

            elif et in ("zone_exited", "ZONE_EXIT"):
                key = (vid, zone)
                enter_ts = zone_enter_times.pop(key, None)
                enter_dt = _parse_ts(enter_ts)
                exit_dt  = _parse_ts(ts)
                if enter_dt and exit_dt:
                    dwell_ms = int((exit_dt - enter_dt).total_seconds() * 1000)
                    if dwell_ms > 0:
                        zone_dwell_ms[zone] = zone_dwell_ms.get(zone, 0) + dwell_ms
                        zone_pairs[zone]    = zone_pairs.get(zone, 0) + 1

        # ── End-of-session fallback ──
        # Any (visitor, zone) still open had no zone_exited event. Close it
        # against the visitor's most recent event timestamp so the dwell isn't lost.
        for (vid, zone), enter_ts in zone_enter_times.items():
            close_ts = last_event_ts.get(vid)
            enter_dt = _parse_ts(enter_ts)
            close_dt = _parse_ts(close_ts)
            if enter_dt and close_dt:
                dwell_ms = int((close_dt - enter_dt).total_seconds() * 1000)
                if dwell_ms > 0:
                    zone_dwell_ms[zone] = zone_dwell_ms.get(zone, 0) + dwell_ms
                    zone_pairs[zone]    = zone_pairs.get(zone, 0) + 1

        total_sessions = len(sessions)

        max_count = max(zone_counts.values()) if zone_counts else 1
        max_dwell = max(zone_dwell_ms.values()) if zone_dwell_ms else 1
        if max_dwell == 0:
            max_dwell = 1

        heatmap = {}
        for z, cnt in zone_counts.items():
            pairs = zone_pairs.get(z, 0)
            total_dwell = zone_dwell_ms.get(z, 0)

            # ── DWELL FIX: divide by paired count, not total enters ──
            avg_dwell = int(total_dwell / pairs) if pairs > 0 else 0

            freq_score  = (cnt / max_count) * 100 if max_count else 0
            dwell_score = (total_dwell / max_dwell) * 100 if max_dwell else 0
            score = int((freq_score * 0.5) + (dwell_score * 0.5))

            heatmap[z] = {
                "frequency": cnt,
                "matched_pairs": pairs,        # diagnostic — should be close to frequency
                "score": score,
                "avg_dwell_ms": avg_dwell,
            }

        data_confidence = "low" if total_sessions < 20 else "high"

        conn.close()
        return JSONResponse(content={
            "store_id": store_id,
            "total_sessions": total_sessions,
            "data_confidence": data_confidence,
            "zones": heatmap,
        })

    except HTTPException:
        raise
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        raise HTTPException(status_code=503, detail=f"error: {str(e)}")