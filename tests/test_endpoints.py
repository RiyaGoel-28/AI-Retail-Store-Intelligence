"""
# PROMPT: Generate tests that verify core API endpoints using actual challenge
# event format (entry/exit with id_token, zone_entered with track_id, queue events).
# CHANGES MADE: Updated all sample events to actual format, updated assertions
# for new field names. Removed duplicate uuid import.
"""
import uuid
import pytest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def _make_sample_events():
    uid = uuid.uuid4().hex[:5]
    return [
        {
            "event_type": "entry",
            "id_token": f"ID_{uid}_1",
            "store_code": "STORE_TEST_001",
            "camera_id": "cam1",
            "event_timestamp": "2026-03-08T18:10:05.120000",
            "is_staff": False,
            "gender_pred": "F",
            "age_pred": 28,
            "age_bucket": "25-34",
            "is_face_hidden": False,
            "group_id": None,
            "group_size": None,
        },
        {
            "event_type": "zone_entered",
            "track_id": 101,
            "store_id": "STORE_TEST_001",
            "camera_id": "CAM2",
            "zone_id": f"ZONE_{uid}_01",
            "zone_name": "Skincare Shelf",
            "zone_type": "SHELF",
            "is_revenue_zone": "Yes",
            "event_time": "2026-03-08T18:10:45.280000",
            "zone_hotspot_x": 412.6,
            "zone_hotspot_y": 238.4,
            "gender": "F",
            "age": 28,
            "age_bucket": "25-34",
        },
        {
            "event_type": "zone_exited",
            "track_id": 101,
            "store_id": "STORE_TEST_001",
            "camera_id": "CAM2",
            "zone_id": f"ZONE_{uid}_01",
            "zone_name": "Skincare Shelf",
            "zone_type": "SHELF",
            "is_revenue_zone": "Yes",
            "event_time": "2026-03-08T18:11:18.720000",
            "zone_hotspot_x": 418.2,
            "zone_hotspot_y": 241.0,
            "gender": "F",
            "age": 28,
            "age_bucket": "25-34",
        },
        {
            "queue_event_id": str(uuid.uuid4()),
            "event_type": "queue_completed",
            "track_id": 101,
            "store_id": "STORE_TEST_001",
            "camera_id": "CAM6",
            "zone_id": f"ZONE_{uid}_BILLING",
            "zone_name": "Billing Counter Queue",
            "zone_type": "BILLING",
            "is_revenue_zone": "Yes",
            "queue_join_ts": "2026-03-08T18:13:05.080000",
            "queue_served_ts": "2026-03-08T18:13:13.240000",
            "queue_exit_ts": "2026-03-08T18:15:31.840000",
            "wait_seconds": 8,
            "queue_position_at_join": 2,
            "abandoned": False,
            "zone_hotspot_x": 602.8,
            "zone_hotspot_y": 183.4,
            "gender": "F",
            "age": 28,
            "age_bucket": "25-34",
        },
    ]


def test_ingest_and_endpoints():
    sample = _make_sample_events()
    resp = client.post("/events/ingest", json={"events": sample})
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] == 4

    # metrics
    m = client.get("/stores/STORE_TEST_001/metrics")
    assert m.status_code == 200
    mm = m.json()
    assert mm["unique_visitors"] >= 1
    assert "conversion_rate" in mm

    # funnel
    f = client.get("/stores/STORE_TEST_001/funnel")
    assert f.status_code == 200
    ff = f.json()
    assert ff["sessions"] >= 1
    assert ff["stages"]["purchase"]["count"] >= 0

    # heatmap
    h = client.get("/stores/STORE_TEST_001/heatmap")
    assert h.status_code == 200
    hh = h.json()
    assert hh["total_sessions"] >= 1

    # anomalies
    a = client.get("/stores/STORE_TEST_001/anomalies")
    assert a.status_code == 200
    aa = a.json()
    assert isinstance(aa.get("anomalies"), list)