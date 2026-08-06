"""
# PROMPT: Generate tests for schema validation using actual challenge event format
# and session reconstruction with entry/exit/zone_entered events.
# CHANGES MADE: Updated to use model_validate instead of parse_obj. Test events
# use actual format fields (id_token, store_code, event_timestamp).
# Fixed POS tests to create table if missing (CI environment).
"""
from app.schemas import Event
from app.sessions import reconstruct_sessions
from datetime import datetime, timedelta
import json


def test_schema_validation_entry():
    ev = {
        "event_type": "entry",
        "id_token": "ID_60001",
        "store_code": "store_1076",
        "camera_id": "cam1",
        "event_timestamp": "2026-03-08T18:10:05.120000",
        "is_staff": False,
        "gender_pred": "F",
        "age_pred": 28,
        "age_bucket": "25-34",
        "is_face_hidden": False,
        "group_id": None,
        "group_size": None,
    }
    e = Event.model_validate(ev)
    assert e.event_type == "entry"
    assert e.id_token == "ID_60001"
    assert e.get_store_id() == "store_1076"
    assert e.get_visitor_id() == "ID_60001"


def test_schema_validation_zone():
    ev = {
        "event_type": "zone_entered",
        "track_id": 101,
        "store_id": "ST1076",
        "camera_id": "CAM2",
        "zone_id": "PURPLLE_MUM_1076_Z01",
        "zone_name": "Left Shelf",
        "zone_type": "SHELF",
        "is_revenue_zone": "Yes",
        "event_time": "2026-03-08T18:10:45.280000",
        "zone_hotspot_x": 412.6,
        "zone_hotspot_y": 238.4,
        "gender": "F",
        "age": 28,
        "age_bucket": "25-34",
    }
    e = Event.model_validate(ev)
    assert e.event_type == "zone_entered"
    assert e.get_visitor_id() == "101"
    assert e.get_timestamp() == "2026-03-08T18:10:45.280000"


def test_reconstruct_sessions_reentry_and_exit():
    now = datetime.utcnow()
    rows = []
    rows.append(json.dumps({"id_token": "V1", "event_type": "entry", "event_timestamp": (now - timedelta(minutes=40)).isoformat()}))
    rows.append(json.dumps({"track_id": 101, "event_type": "zone_entered", "zone_name": "Left Shelf", "event_time": (now - timedelta(minutes=39)).isoformat()}))
    rows.append(json.dumps({"id_token": "V1", "event_type": "exit", "event_timestamp": (now - timedelta(minutes=38)).isoformat()}))
    rows.append(json.dumps({"id_token": "V1", "event_type": "entry", "event_timestamp": (now - timedelta(minutes=30)).isoformat()}))
    rows.append(json.dumps({"track_id": 102, "event_type": "zone_entered", "zone_name": "Center Display", "event_time": (now - timedelta(minutes=29)).isoformat()}))
    sessions = reconstruct_sessions(rows)
    assert len(sessions) >= 2


def test_staff_excluded_from_sessions():
    rows = [json.dumps({"event_type": "entry", "id_token": "STAFF_1", "is_staff": True})]
    sessions = reconstruct_sessions(rows)
    assert len(sessions) == 0


def test_invalid_json_skipped():
    sessions = reconstruct_sessions(["not valid json", "{bad"])
    assert sessions == []


def test_no_visitor_id_skipped():
    rows = [json.dumps({"event_type": "entry", "is_staff": False})]
    sessions = reconstruct_sessions(rows)
    assert sessions == []


def test_queue_completed_marks_purchased():
    rows = [json.dumps({
        "event_type": "queue_completed", "track_id": "VIS_Q1", "is_staff": False,
        "queue_join_ts": "2026-06-03T10:00:00Z",
    })]
    sessions = reconstruct_sessions(rows)
    assert sessions[0]["purchased"] is True
    assert sessions[0]["queued"] is True


def test_queue_abandoned_not_purchased():
    rows = [json.dumps({
        "event_type": "queue_abandoned", "track_id": "VIS_Q2", "is_staff": False,
        "queue_join_ts": "2026-06-03T10:00:00Z",
    })]
    sessions = reconstruct_sessions(rows)
    assert sessions[0]["queued"] is True
    assert sessions[0]["purchased"] is False


def test_billing_timestamps_cleaned():
    rows = [json.dumps({
        "event_type": "queue_completed", "track_id": "VIS_Q3", "is_staff": False,
        "queue_join_ts": "2026-06-03T10:00:00Z",
    })]
    sessions = reconstruct_sessions(rows)
    for s in sessions:
        assert "billing_timestamps" not in s


def test_uppercase_event_types():
    rows = [
        json.dumps({"event_type": "ENTRY", "visitor_id": "VIS_UC", "is_staff": False}),
        json.dumps({"event_type": "ZONE_ENTER", "visitor_id": "VIS_UC", "is_staff": False}),
        json.dumps({"event_type": "BILLING_QUEUE_JOIN", "visitor_id": "VIS_UC", "is_staff": False, "timestamp": "2026-06-03T10:00:00Z"}),
    ]
    sessions = reconstruct_sessions(rows)
    s = sessions[0]
    assert s["entered"] is True
    assert s["zone_visit"] is True
    assert s["queued"] is True


def test_multiple_visitors():
    rows = [
        json.dumps({"event_type": "entry", "id_token": "A", "is_staff": False}),
        json.dumps({"event_type": "entry", "id_token": "B", "is_staff": False}),
    ]
    sessions = reconstruct_sessions(rows)
    assert len(sessions) == 2


def test_parse_ts_variants():
    from app.sessions import _parse_ts
    assert _parse_ts("2026-06-03T10:00:00Z").year == 2026
    assert _parse_ts("2026-06-03T10:00:00").tzinfo is not None
    assert _parse_ts("2026-06-03T10:00:00+05:30").year == 2026
    assert _parse_ts("not-a-date") is not None


def _ensure_pos_table(conn):
    """Create pos_transactions table if it doesn't exist (CI environment)."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(pos_transactions)").fetchall()]
    if not cols:
        conn.execute("""CREATE TABLE pos_transactions (
            order_id TEXT, store_id TEXT, order_date TEXT, order_time TEXT,
            basket_value REAL
        )""")
        conn.commit()
        cols = ["order_id", "store_id", "order_date", "order_time", "basket_value"]
    return cols


def test_pos_correlation_marks_purchased():
    """Test POS correlation: visitor in queue within 5 min of POS transaction."""
    import os, sqlite3
    pos_db = os.path.join("data", "store_intelligence.db")
    os.makedirs("data", exist_ok=True)

    conn = sqlite3.connect(pos_db)
    cols = _ensure_pos_table(conn)
    conn.execute("DELETE FROM pos_transactions WHERE store_id='TEST_POS'")
    col_str = ", ".join(cols)
    placeholders = ", ".join(["?"] * len(cols))
    values = []
    for c in cols:
        if c == "store_id": values.append("TEST_POS")
        elif c == "order_id": values.append("TXN1")
        elif c == "order_date": values.append("03-06-2026")
        elif c == "order_time": values.append("10:05:00")
        elif c == "basket_value": values.append(500.0)
        else: values.append(None)
    conn.execute(f"INSERT INTO pos_transactions ({col_str}) VALUES ({placeholders})", values)
    conn.commit()
    conn.close()

    rows = [
        json.dumps({"event_type": "entry", "id_token": "VIS_POS1", "is_staff": False}),
        json.dumps({
            "event_type": "queue_abandoned", "track_id": "VIS_POS1", "is_staff": False,
            "queue_join_ts": "2026-06-03T10:02:00",
        }),
    ]
    sessions = reconstruct_sessions(rows, store_id="TEST_POS")
    vis = [s for s in sessions if s["visitor_id"] == "VIS_POS1"]
    assert any(s["purchased"] for s in vis)


def test_pos_no_match_outside_window():
    """POS transaction too far from queue time — no purchase."""
    import os, sqlite3
    pos_db = os.path.join("data", "store_intelligence.db")
    os.makedirs("data", exist_ok=True)

    conn = sqlite3.connect(pos_db)
    cols = _ensure_pos_table(conn)
    conn.execute("DELETE FROM pos_transactions WHERE store_id='TEST_POS2'")
    col_str = ", ".join(cols)
    placeholders = ", ".join(["?"] * len(cols))
    values = []
    for c in cols:
        if c == "store_id": values.append("TEST_POS2")
        elif c == "order_id": values.append("TXN2")
        elif c == "order_date": values.append("03-06-2026")
        elif c == "order_time": values.append("11:00:00")
        elif c == "basket_value": values.append(300.0)
        else: values.append(None)
    conn.execute(f"INSERT INTO pos_transactions ({col_str}) VALUES ({placeholders})", values)
    conn.commit()
    conn.close()

    rows = [
        json.dumps({
            "event_type": "queue_abandoned", "track_id": "VIS_POS2", "is_staff": False,
            "queue_join_ts": "2026-06-03T10:00:00",
        }),
    ]
    sessions = reconstruct_sessions(rows, store_id="TEST_POS2")
    assert not any(s["purchased"] for s in sessions)


def test_load_pos_no_db():
    """When POS DB doesn't exist, sessions still work."""
    from app.sessions import _load_pos_transactions
    result = _load_pos_transactions("NONEXISTENT_STORE")
    assert isinstance(result, list)


def test_sessions_with_store_id_no_pos():
    rows = [json.dumps({"event_type": "entry", "id_token": "VIS_NP", "is_staff": False})]
    sessions = reconstruct_sessions(rows, store_id="NO_SUCH_STORE_999")
    assert len(sessions) == 1
    assert sessions[0]["purchased"] is False


def test_schema_validation_reentry():
    ev = {
        "event_type": "reentry",
        "id_token": "ID_REENTRY_1",
        "store_code": "ST1076",
        "camera_id": "cam1",
        "event_timestamp": "2026-06-03T18:10:05.120000",
        "is_staff": False,
    }
    e = Event.model_validate(ev)
    assert e.event_type == "reentry"
    assert e.get_visitor_id() == "ID_REENTRY_1"


def test_schema_validation_queue_joined():
    ev = {
        "event_type": "queue_joined",
        "track_id": 42,
        "store_id": "ST1076",
        "camera_id": "CAM6",
        "zone_id": "BILLING",
        "zone_name": "Billing Counter Queue",
        "queue_join_ts": "2026-06-03T18:13:05.080000",
        "queue_position_at_join": 3,
    }
    e = Event.model_validate(ev)
    assert e.event_type == "queue_joined"
    assert e.get_visitor_id() == "42"


def test_schema_validation_zone_dwell():
    ev = {
        "event_type": "zone_dwell",
        "track_id": 7,
        "store_id": "ST1076",
        "camera_id": "CAM2",
        "zone_id": "MAKEUP",
        "zone_name": "Makeup Aisle",
        "event_time": "2026-06-03T18:14:00.000000",
        "dwell_ms": 32000,
    }
    e = Event.model_validate(ev)
    assert e.event_type == "zone_dwell"
    assert e.dwell_ms == 32000


def test_reconstruct_sessions_with_reentry():
    rows = [
        json.dumps({"event_type": "reentry", "id_token": "VIS_RE1", "is_staff": False}),
    ]
    sessions = reconstruct_sessions(rows)
    assert len(sessions) == 1
    assert sessions[0]["entered"] is True