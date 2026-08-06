"""
# PROMPT: Rewrite metrics endpoint for actual challenge data: entry/exit with id_token,
# zone_entered/zone_exited with track_id, queue_completed/queue_abandoned, POS with DD-MM-YYYY dates
# CHANGES MADE: Queue depth from queue_joined - queue_completed/abandoned. Reentry counted as entry.
# Staff excluded. POS correlation with actual CSV format.
# FIX: Use zone_id as stable key for dwell pairing. Added /staff-stats endpoint.
# STAFF FIX: Set-based staff filtering — a visitor flagged as staff on ANY event is excluded
# from all customer counts. /staff-stats no longer double-counts the same visitor as both.
# DWELL FIX: End-of-session fallback — close any zone_entered with no matching zone_exited
# against the visitor's last seen timestamp. Mirrors the fallback in heatmap.py so the
# Avg Dwell Time tile and the heatmap card now show consistent numbers.
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
import sqlite3
import os
from datetime import datetime, timezone
import json

router = APIRouter()
DB_PATH = os.path.join("data", "events.db")
POS_DB_PATH = os.path.join("data", "store_intelligence.db")


def _connect():
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        raise HTTPException(status_code=503, detail="db_unavailable")


def _staff_visitor_set(cur, store_id: str) -> set:
    """Return the set of visitor_ids that are staff for this store
    (anyone with at least one is_staff=1 event)."""
    rows = cur.execute(
        "SELECT DISTINCT visitor_id FROM events "
        "WHERE store_id = ? AND is_staff = 1 "
        "AND visitor_id IS NOT NULL AND visitor_id != ''",
        (store_id,),
    ).fetchall()
    return {r["visitor_id"] for r in rows}


@router.get("/stores/{store_id}/staff-stats")
def staff_stats(store_id: str):
    """Return staff vs customer detection counts for the store.
    A visitor is counted as staff if ANY of their events has is_staff=1.
    customer_count = all_distinct_visitors − staff_visitors  (no double counting)
    """
    conn = _connect()
    try:
        cur = conn.cursor()

        # All distinct visitors for this store
        all_rows = cur.execute(
            "SELECT DISTINCT visitor_id FROM events "
            "WHERE store_id = ? AND visitor_id IS NOT NULL AND visitor_id != ''",
            (store_id,),
        ).fetchall()
        all_visitors = {r["visitor_id"] for r in all_rows}

        # Staff = anyone flagged on any event
        staff_visitors = _staff_visitor_set(cur, store_id)

        # Customers = the rest
        customer_visitors = all_visitors - staff_visitors

        staff_count    = len(staff_visitors)
        customer_count = len(customer_visitors)
        total_people   = staff_count + customer_count

        staff_events_row = cur.execute(
            "SELECT COUNT(*) as cnt FROM events WHERE store_id = ? AND is_staff = 1",
            (store_id,),
        ).fetchone()
        staff_events = staff_events_row["cnt"] if staff_events_row else 0

        conn.close()
        return JSONResponse(content={
            "store_id": store_id,
            "staff_count": staff_count,
            "customer_count": customer_count,
            "total_people": total_people,
            "staff_events": staff_events,
            "exclusion_pct": round((staff_count / total_people) * 100, 2)
                if total_people > 0 else 0.0,
        })
    except HTTPException:
        raise
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        raise HTTPException(status_code=503, detail=f"error: {str(e)}")


@router.get("/stores/{store_id}/metrics")
def store_metrics(store_id: str):
    """Metrics: unique visitors, conversion rate, avg dwell per zone,
    queue depth, abandonment rate. Excludes staff via set-based filter."""
    conn = _connect()

    try:
        cur = conn.cursor()
        now = datetime.now(timezone.utc)
        today_prefix = now.strftime("%Y-%m-%d")

        cur.execute("""
            SELECT event_type, visitor_id, zone_id, zone_name, is_staff, timestamp, payload
            FROM events
            WHERE store_id = ? AND timestamp LIKE ?
        """, (store_id, f"{today_prefix}%"))
        rows = cur.fetchall()

        if not rows:
            cur.execute("""
                SELECT event_type, visitor_id, zone_id, zone_name, is_staff, timestamp, payload
                FROM events WHERE store_id = ?
            """, (store_id,))
            rows = cur.fetchall()

        if not rows:
            conn.close()
            return JSONResponse(content={
                "store_id": store_id,
                "unique_visitors": 0,
                "conversion_rate": 0.0,
                "avg_dwell_per_zone": {},
                "queue_depth": 0,
                "abandonment_rate": 0.0
            })

        # ── Set-based staff filter ──
        # A visitor flagged as staff on any event is staff across the board.
        staff_visitor_ids = {
            row["visitor_id"] for row in rows
            if row["is_staff"] and row["visitor_id"]
        }

        visitors = set()
        zone_enter_times = {}
        zone_dwell_total = {}
        zone_dwell_count = {}
        zone_display_names = {}
        queue_joined = set()
        queue_exited = set()
        abandonments = 0
        billing_visitors = set()

        # Track the last seen timestamp per visitor (used by the dwell fallback below).
        last_event_ts = {}

        for row in rows:
            event_type = row["event_type"]
            visitor_id = row["visitor_id"]

            # Skip every row of any visitor flagged as staff
            if visitor_id in staff_visitor_ids:
                continue

            # Record last seen timestamp for the dwell fallback
            if visitor_id and row["timestamp"]:
                last_event_ts[visitor_id] = row["timestamp"]

            # Count unique visitors from entry AND reentry events
            if event_type in ("entry", "ENTRY", "reentry", "REENTRY"):
                if visitor_id:
                    visitors.add(visitor_id)

            # Zone dwell — use zone_id as STABLE key for pairing
            if event_type in ("zone_entered", "ZONE_ENTER"):
                zone_key = row["zone_id"] or row["zone_name"] or ""
                display_name = row["zone_name"] or row["zone_id"] or zone_key
                ts = row["timestamp"]
                if visitor_id and zone_key:
                    zone_enter_times[(visitor_id, zone_key)] = ts
                    zone_display_names[zone_key] = display_name

            if event_type in ("zone_exited", "ZONE_EXIT"):
                zone_key = row["zone_id"] or row["zone_name"] or ""
                display_name = row["zone_name"] or row["zone_id"] or zone_key
                ts = row["timestamp"]
                key = (visitor_id, zone_key)
                if key in zone_enter_times and ts:
                    try:
                        enter_dt = datetime.fromisoformat(zone_enter_times[key])
                        exit_dt = datetime.fromisoformat(ts)
                        dwell_ms = int((exit_dt - enter_dt).total_seconds() * 1000)
                        if dwell_ms > 0:
                            zone_dwell_total[zone_key] = zone_dwell_total.get(zone_key, 0) + dwell_ms
                            zone_dwell_count[zone_key] = zone_dwell_count.get(zone_key, 0) + 1
                            zone_display_names[zone_key] = display_name
                    except Exception:
                        pass
                    del zone_enter_times[key]

            # Queue events
            if event_type in ("queue_joined", "queue_completed", "queue_abandoned",
                              "BILLING_QUEUE_JOIN"):
                if visitor_id:
                    queue_joined.add(visitor_id)

            if event_type in ("queue_completed", "queue_abandoned", "BILLING_QUEUE_ABANDON"):
                if visitor_id:
                    queue_exited.add(visitor_id)
                if event_type in ("queue_abandoned", "BILLING_QUEUE_ABANDON"):
                    abandonments += 1
                elif event_type == "queue_completed":
                    billing_visitors.add(visitor_id)

        # ── DWELL FIX: end-of-session fallback ──
        # For any zone_entered that never got a matching zone_exited (e.g. clip ended,
        # track timed out, or visitor exited the store while still in zone), close
        # the dwell against the visitor's last observed timestamp. Mirrors the
        # fallback in app/heatmap.py so the tile and the heatmap card agree.
        for (vid, zone_key), enter_ts in zone_enter_times.items():
            close_ts = last_event_ts.get(vid)
            if not (enter_ts and close_ts):
                continue
            try:
                enter_dt = datetime.fromisoformat(enter_ts)
                close_dt = datetime.fromisoformat(close_ts)
                dwell_ms = int((close_dt - enter_dt).total_seconds() * 1000)
                if dwell_ms > 0:
                    zone_dwell_total[zone_key] = zone_dwell_total.get(zone_key, 0) + dwell_ms
                    zone_dwell_count[zone_key] = zone_dwell_count.get(zone_key, 0) + 1
            except Exception:
                pass

        # If no entry events found, count all unique non-staff visitor_ids
        if not visitors:
            for row in rows:
                vid = row["visitor_id"]
                if vid and vid not in staff_visitor_ids:
                    visitors.add(vid)

        # POS-based conversion
        converted_visitors = set()
        try:
            if os.path.exists(POS_DB_PATH):
                pos_conn = sqlite3.connect(POS_DB_PATH, timeout=5)
                pos_conn.row_factory = sqlite3.Row
                pos_rows = pos_conn.execute("""
                    SELECT DISTINCT order_id, order_date, order_time
                    FROM pos_transactions WHERE store_id = ?
                """, (store_id,)).fetchall()

                for pos in pos_rows:
                    try:
                        od = pos['order_date']
                        ot = pos['order_time']
                        if '-' in od and len(od.split('-')[0]) == 2:
                            parts = od.split('-')
                            od = f"{parts[2]}-{parts[1]}-{parts[0]}"
                        txn_time = f"{od}T{ot}" if ot else od

                        bv_rows = cur.execute("""
                            SELECT DISTINCT visitor_id FROM events
                            WHERE store_id = ?
                            AND event_type IN ('queue_completed', 'queue_joined', 'BILLING_QUEUE_JOIN')
                            AND is_staff = 0
                            AND timestamp BETWEEN datetime(?, '-5 minutes') AND datetime(?)
                        """, (store_id, txn_time, txn_time)).fetchall()
                        for bv in bv_rows:
                            vid = bv['visitor_id']
                            if vid and vid not in staff_visitor_ids:
                                converted_visitors.add(vid)
                    except Exception:
                        pass
                pos_conn.close()
        except Exception:
            pass

        converted_visitors.update(billing_visitors)

        unique_visitors = len(visitors)
        converted = len(converted_visitors)
        conversion_rate = round((converted / unique_visitors) * 100, 2) if unique_visitors else 0.0

        # Build avg dwell with display names
        avg_dwell_per_zone = {}
        for zone_key in zone_dwell_total:
            if zone_dwell_count.get(zone_key, 0) > 0:
                display = zone_display_names.get(zone_key, zone_key)
                avg_dwell_per_zone[display] = round(
                    zone_dwell_total[zone_key] / zone_dwell_count[zone_key]
                )

        queue_depth = max(0, len(queue_joined) - len(queue_exited))
        total_queue = len(queue_joined)
        abandonment_rate = round((abandonments / total_queue) * 100, 2) if total_queue else 0.0

        conn.close()
        return JSONResponse(content={
            "store_id": store_id,
            "unique_visitors": unique_visitors,
            "conversion_rate": conversion_rate,
            "avg_dwell_per_zone": avg_dwell_per_zone,
            "queue_depth": queue_depth,
            "abandonment_rate": abandonment_rate
        })

    except HTTPException:
        raise
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        raise HTTPException(status_code=503, detail=f"error: {str(e)}")