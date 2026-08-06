"""
# PROMPT: Update health endpoint to parse actual timestamp format (ISO with microseconds, no Z)
# CHANGES MADE: Handle "2026-03-08T18:10:05.120000" format, fixed STALE_FEED detection.
# FIX: Filter out test/internal stores from health display (STORE_CONV_*, STORE_IDEMP_*, STORE_TEST_*)
# FIX: Defensive lag math — future-dated events (simulated/eval data) no longer
#      report bogus 126,000-minute lag from Python floor-division on negatives.
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse
import sqlite3
import os
import re
from datetime import datetime, timezone

router = APIRouter()
DB_PATH = os.path.join("data", "events.db")

# Test store patterns to filter from health display
TEST_STORE_PATTERN = re.compile(
    r"^(STORE_CONV_|STORE_IDEMP_|STORE_TEST_|STORE_EDGE_)",
    re.IGNORECASE
)


def _connect():
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


@router.get("/health")
def health_check():
    conn = _connect()
    if conn is None:
        return JSONResponse(status_code=503, content={
            "status": "ERROR",
            "message": "db_unavailable",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stores": {}
        })

    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY, store_id TEXT, camera_id TEXT,
                visitor_id TEXT, event_type TEXT, timestamp TEXT,
                zone_id TEXT, zone_name TEXT, is_staff INTEGER DEFAULT 0, payload TEXT
            )
        """)
        conn.commit()

        cur = conn.cursor()
        cur.execute("""
            SELECT store_id, MAX(timestamp) as last_event_at, COUNT(*) as event_count
            FROM events GROUP BY store_id
        """)
        rows = cur.fetchall()

        now = datetime.now(timezone.utc)
        store_status = {}

        for row in rows:
            store_id = row["store_id"]
            last_event_at = row["last_event_at"]
            event_count = row["event_count"]

            # Skip test/internal stores from health display
            if TEST_STORE_PATTERN.match(store_id):
                continue

            try:
                ts_str = last_event_at.replace("Z", "+00:00")
                if "+" not in ts_str and ts_str.count("-") <= 2:
                    ts_str = ts_str + "+00:00"
                last_ts = datetime.fromisoformat(ts_str)
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=timezone.utc)

                lag_seconds = (now - last_ts).total_seconds()

                # Defensive: future-dated events (simulated/eval data) → treat as fresh.
                # Previously: int(negative // 60) produced huge positive numbers due to
                # Python's floor-division on negatives, making /health show ~126,000 min.
                if lag_seconds < 0:
                    lag_minutes = 0
                    status = "OK"
                else:
                    lag_minutes = int(lag_seconds // 60)
                    status = "STALE_FEED" if lag_minutes > 10 else "OK"

                store_status[store_id] = {
                    "last_event_at": last_event_at,
                    "lag_minutes": lag_minutes,
                    "event_count": event_count,
                    "status": status,
                }
            except Exception:
                store_status[store_id] = {
                    "last_event_at": last_event_at,
                    "lag_minutes": None,
                    "event_count": event_count,
                    "status": "UNKNOWN"
                }

        conn.close()
        return {"status": "OK", "timestamp": now.isoformat(), "stores": store_status}

    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        return JSONResponse(status_code=503, content={
            "status": "ERROR", "message": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(), "stores": {}
        })