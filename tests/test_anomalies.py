"""
# PROMPT: Generate tests for anomaly detection using actual queue_completed/queue_abandoned
# events with queue_position_at_join field for queue spike detection.
# CHANGES MADE: Updated to actual format. Removed _clean_db to avoid wiping events.db.
"""
import pytest
from fastapi.testclient import TestClient
from datetime import datetime, timezone
import uuid

from app.main import app

client = TestClient(app)


def _uid():
    return uuid.uuid4().hex[:5]


def make_entry(eid, vid):
    return {
        "event_id": eid,
        "event_type": "entry",
        "id_token": vid,
        "store_code": "STORE_BLR_002",
        "camera_id": "cam1",
        "event_timestamp": datetime.now(timezone.utc).isoformat(),
        "is_staff": False,
        "gender_pred": "M", "age_pred": 30, "age_bucket": "25-34",
        "is_face_hidden": False, "group_id": None, "group_size": None,
    }


def make_queue(eid, track_id, qd=8, abandoned=False):
    return {
        "event_id": eid,
        "queue_event_id": str(uuid.uuid4()),
        "event_type": "queue_abandoned" if abandoned else "queue_completed",
        "track_id": track_id,
        "store_id": "STORE_BLR_002",
        "camera_id": "CAM6",
        "zone_id": "BILLING_01",
        "zone_name": "Billing Counter Queue",
        "zone_type": "BILLING",
        "is_revenue_zone": "Yes",
        "queue_join_ts": datetime.now(timezone.utc).isoformat(),
        "queue_served_ts": None if abandoned else datetime.now(timezone.utc).isoformat(),
        "queue_exit_ts": datetime.now(timezone.utc).isoformat(),
        "wait_seconds": 15,
        "queue_position_at_join": qd,
        "abandoned": abandoned,
        "zone_hotspot_x": 600.0, "zone_hotspot_y": 180.0,
        "gender": "M", "age": 30, "age_bucket": "25-34",
    }


class TestQueueSpike:
    def test_queue_spike_detected(self):
        events = [make_queue(f"qs_{i}_{_uid()}", i, qd=8) for i in range(6)]
        client.post("/events/ingest", json={"events": events})
        resp = client.get("/stores/STORE_BLR_002/anomalies")
        data = resp.json()
        types = [a["type"] for a in data.get("anomalies", [])]
        assert "BILLING_QUEUE_SPIKE" in types

    def test_queue_spike_severity(self):
        events = [make_queue(f"qs2_{i}_{_uid()}", i, qd=40) for i in range(10)]
        client.post("/events/ingest", json={"events": events})
        resp = client.get("/stores/STORE_BLR_002/anomalies")
        data = resp.json()
        severities = [a.get("severity") for a in data.get("anomalies", []) if a.get("type") == "BILLING_QUEUE_SPIKE"]
        assert any(s in ("WARN", "CRITICAL") for s in severities)


class TestConversionDrop:
    def test_low_conversion_detected(self):
        uid = _uid()
        store = f"STORE_CONV_{uid}"
        events = [
            {
                "event_id": f"cd_{i}_{uid}",
                "event_type": "entry",
                "id_token": f"VIS_{i}",
                "store_code": store,
                "camera_id": "cam1",
                "event_timestamp": datetime.now(timezone.utc).isoformat(),
                "is_staff": False,
                "gender_pred": "M", "age_pred": 30, "age_bucket": "25-34",
                "is_face_hidden": False, "group_id": None, "group_size": None,
            }
            for i in range(20)
        ]
        client.post("/events/ingest", json={"events": events})
        resp = client.get(f"/stores/{store}/anomalies")
        data = resp.json()
        types = [a["type"] for a in data.get("anomalies", [])]
        assert "CONVERSION_DROP" in types


class TestAnomalyFormat:
    def test_suggested_action_present(self):
        events = [make_entry(f"a1_{_uid()}", "V1")]
        client.post("/events/ingest", json={"events": events})
        resp = client.get("/stores/STORE_BLR_002/anomalies")
        data = resp.json()
        for a in data.get("anomalies", []):
            assert "suggested_action" in a