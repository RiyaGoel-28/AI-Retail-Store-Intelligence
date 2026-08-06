"""
# PROMPT: Rewrite funnel endpoint for actual challenge data format with drop-off percentages
# CHANGES MADE: Use actual event_types (entry/exit/zone_entered/queue_completed/queue_abandoned),
# de-duplicate re-entries, compute drop-off % between stages.
# FIX: queue_abandoned counted in billing_queue but NOT purchase — shows real drop-off.
# queue_joined also counted. Funnel base = entries. Added abandonment stats.
# STAFF FIX: Set-based staff filter so a visitor flagged on any event is excluded everywhere.
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
import sqlite3
import os
import json
from datetime import datetime, timezone

router = APIRouter()
DB_PATH = os.path.join("data", "events.db")


def _connect():
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        raise HTTPException(status_code=503, detail="db_unavailable")


@router.get("/stores/{store_id}/funnel")
def store_funnel(store_id: str):
    conn = _connect()

    try:
        cur = conn.cursor()

        cur.execute("""
            SELECT event_type, visitor_id, zone_id, zone_name, is_staff, timestamp, payload
            FROM events WHERE store_id = ? ORDER BY timestamp
        """, (store_id,))
        rows = cur.fetchall()

        # ── Set-based staff filter ──
        staff_visitor_ids = {
            row["visitor_id"] for row in rows
            if row["is_staff"] and row["visitor_id"]
        }

        # Build per-visitor session data
        visitor_data = {}

        for row in rows:
            vid = row["visitor_id"]
            if not vid or vid in staff_visitor_ids:
                continue

            if vid not in visitor_data:
                visitor_data[vid] = {
                    "entered": False,
                    "zone_visit": False,
                    "queued": False,
                    "purchased": False,
                    "abandoned": False,
                }

            et = row["event_type"]
            if et in ("entry", "ENTRY", "reentry", "REENTRY"):
                visitor_data[vid]["entered"] = True
            elif et in ("zone_entered", "ZONE_ENTER"):
                visitor_data[vid]["zone_visit"] = True
            elif et in ("queue_joined", "queue_completed", "queue_abandoned",
                        "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON"):
                # All queue events = visitor reached billing queue
                visitor_data[vid]["queued"] = True

                if et in ("queue_completed",):
                    visitor_data[vid]["purchased"] = True
                elif et in ("queue_abandoned", "BILLING_QUEUE_ABANDON"):
                    visitor_data[vid]["abandoned"] = True

        # Edge case: visitor who abandoned then re-queued and completed —
        # purchased flag from queue_completed takes precedence.

        sessions = list(visitor_data.values())
        total = len(sessions)

        entries = sum(1 for s in sessions if s["entered"])
        zone_visits = sum(1 for s in sessions if s["zone_visit"])
        queued = sum(1 for s in sessions if s["queued"])
        purchased = sum(1 for s in sessions if s["purchased"])
        abandoned = sum(1 for s in sessions if s["abandoned"] and not s["purchased"])

        # Funnel base = entries if available, else total sessions
        funnel_base = entries if entries > 0 else total

        def pct(n):
            return round((n / funnel_base) * 100, 2) if funnel_base else 0.0

        def dropoff(prev, curr):
            return round(((prev - curr) / prev) * 100, 2) if prev else 0.0

        conn.close()
        return JSONResponse(content={
            "store_id": store_id,
            "sessions": total,
            "funnel_base": funnel_base,
            "stages": {
                "entry": {
                    "count": entries,
                    "pct": pct(entries),
                    "drop_off_pct": 0.0,
                },
                "zone_visit": {
                    "count": zone_visits,
                    "pct": pct(zone_visits),
                    "drop_off_pct": dropoff(entries, zone_visits),
                },
                "billing_queue": {
                    "count": queued,
                    "pct": pct(queued),
                    "drop_off_pct": dropoff(zone_visits, queued),
                },
                "purchase": {
                    "count": purchased,
                    "pct": pct(purchased),
                    "drop_off_pct": dropoff(queued, purchased),
                },
            },
            "abandonment": {
                "count": abandoned,
                "rate_pct": round((abandoned / queued) * 100, 2) if queued else 0.0,
            },
        })

    except HTTPException:
        raise
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        raise HTTPException(status_code=503, detail=f"error: {str(e)}")