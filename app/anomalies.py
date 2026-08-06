"""
# PROMPT: Rewrite anomaly detection for actual queue_completed/queue_abandoned events
# CHANGES MADE: Use queue_position_at_join for queue spike, zone_entered for dead zone detection,
# fixed baseline_purchases counting from queue_completed events, severity levels.
# STAFF FIX: Set-based staff filter applied to both current and baseline windows.
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
import sqlite3
import os
import json
from datetime import datetime, timedelta, timezone

router = APIRouter()
DB_PATH = os.path.join("data", "events.db")
LAYOUT_PATH = os.path.join("data", "store_layout.json")


def _connect():
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        raise HTTPException(status_code=503, detail="db_unavailable")


def _load_known_zones(store_id: str):
    try:
        with open(LAYOUT_PATH, "r") as f:
            layout = json.load(f)
        if isinstance(layout, dict):
            store_layout = layout.get(store_id, layout)
            if isinstance(store_layout, dict):
                zones = list(store_layout.get("zones", {}).keys())
                if zones:
                    return zones
        if isinstance(layout, list):
            return [
                z.get("zone_id") or z.get("name") or z.get("zone_name")
                for z in layout
                if z.get("zone_id") or z.get("name") or z.get("zone_name")
            ]
    except Exception:
        pass
    return []


def _staff_set(rows):
    """Build the set of visitor_ids that were ever flagged as staff in `rows`."""
    return {
        row["visitor_id"] for row in rows
        if row["is_staff"] and row["visitor_id"]
    }


@router.get("/stores/{store_id}/anomalies")
def store_anomalies(store_id: str):
    conn = _connect()

    try:
        cur = conn.cursor()
        now = datetime.now(timezone.utc)
        window_start = (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S")

        # Get recent events
        cur.execute("""
            SELECT event_type, visitor_id, zone_id, zone_name, is_staff, timestamp, payload
            FROM events WHERE store_id = ? AND timestamp >= ? ORDER BY timestamp
        """, (store_id, window_start))
        rows = cur.fetchall()

        # If no results with time filter, get all events for this store
        if not rows:
            cur.execute("""
                SELECT event_type, visitor_id, zone_id, zone_name, is_staff, timestamp, payload
                FROM events WHERE store_id = ? ORDER BY timestamp
            """, (store_id,))
            rows = cur.fetchall()

        # ── Set-based staff filter ──
        staff_visitor_ids = _staff_set(rows)

        queue_depths = []
        zone_visits = {}
        visitors = set()

        for row in rows:
            vid = row["visitor_id"]
            if not vid or vid in staff_visitor_ids:
                continue

            visitors.add(vid)
            et = row["event_type"] or ""

            # Queue depth from queue events
            if et in ("queue_completed", "queue_abandoned"):
                try:
                    payload = json.loads(row["payload"]) if row["payload"] else {}
                    qd = payload.get("queue_position_at_join")
                    if qd is not None:
                        queue_depths.append(int(qd))
                except Exception:
                    pass

            # Zone visits
            if et in ("zone_entered", "ZONE_ENTER"):
                zone = row["zone_name"] or row["zone_id"] or ""
                if zone:
                    zone_visits[zone] = zone_visits.get(zone, 0) + 1

        anomalies_list = []

        # 1. Queue spike: avg queue position > 5
        if queue_depths:
            avg_q = sum(queue_depths) / len(queue_depths)
            if avg_q > 5:
                anomalies_list.append({
                    "type": "BILLING_QUEUE_SPIKE",
                    "severity": "WARN" if avg_q <= 15 else "CRITICAL",
                    "detail": f"avg_queue_position={avg_q:.1f}",
                    "suggested_action": "Open an additional billing register or reassign staff to checkout",
                })

        # 2. Dead zone detection
        known_zones = _load_known_zones(store_id)
        for zone in known_zones:
            if zone not in zone_visits:
                anomalies_list.append({
                    "type": "DEAD_ZONE",
                    "severity": "INFO",
                    "zone": zone,
                    "detail": f"zone={zone} had 0 visits in last 30m",
                    "suggested_action": f"Check camera feed for {zone}, inspect floor placement or signage",
                })

        # 3. Conversion drop: compare to 7-day baseline
        total_visitors = len(visitors)
        current_purchases = sum(
            1 for row in rows
            if row["event_type"] == "queue_completed"
            and row["visitor_id"] not in staff_visitor_ids
        )
        current_conv = (current_purchases / total_visitors) if total_visitors else 0.0

        baseline_start = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
        baseline_end = (now - timedelta(days=6)).strftime("%Y-%m-%dT%H:%M:%S")
        cur.execute("""
            SELECT event_type, visitor_id, is_staff FROM events
            WHERE store_id = ? AND timestamp >= ? AND timestamp < ?
        """, (store_id, baseline_start, baseline_end))
        baseline_rows = cur.fetchall()

        # Baseline window has its own staff set
        baseline_staff = _staff_set(baseline_rows)

        baseline_visitors = set()
        baseline_purchases = 0
        for br in baseline_rows:
            vid = br["visitor_id"]
            if not vid or vid in baseline_staff:
                continue
            baseline_visitors.add(vid)
            if br["event_type"] == "queue_completed":
                baseline_purchases += 1

        baseline_conv = (baseline_purchases / len(baseline_visitors)) if baseline_visitors else 0.0

        if baseline_conv > 0 and current_conv < (baseline_conv * 0.6):
            anomalies_list.append({
                "type": "CONVERSION_DROP",
                "severity": "WARN",
                "detail": f"current={current_conv:.3f} baseline={baseline_conv:.3f}",
                "suggested_action": "Investigate promotion, staffing, or POS issues",
            })
        elif not baseline_visitors and total_visitors >= 10 and current_conv == 0.0:
            anomalies_list.append({
                "type": "CONVERSION_DROP",
                "severity": "WARN",
                "detail": f"current={current_conv:.3f} baseline=none",
                "suggested_action": "Investigate promotions, staff, or POS",
            })

        conn.close()
        return JSONResponse(content={"store_id": store_id, "anomalies": anomalies_list})

    except HTTPException:
        raise
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        raise HTTPException(status_code=503, detail=f"error: {str(e)}")