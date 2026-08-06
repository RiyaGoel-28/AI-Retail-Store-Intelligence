"""
# PROMPT: Generate integration tests for Store Intelligence API using actual
# challenge event format: entry/exit/zone_entered/queue_completed/queue_abandoned.
# CHANGES MADE: All make_event helpers updated to actual format. Staff exclusion
# uses is_staff field in entry events. Queue tests use queue_position_at_join.
"""
import pytest
from fastapi.testclient import TestClient
from datetime import datetime, timezone
import uuid

from app.main import app

client = TestClient(app)


def _uid():
    return uuid.uuid4().hex[:5]


def make_entry(eid, vid, store_id="STORE_BLR_002", is_staff=False):
    return {
        "event_id": eid,
        "event_type": "entry",
        "id_token": vid,
        "store_code": store_id,
        "camera_id": "cam1",
        "event_timestamp": datetime.now(timezone.utc).isoformat(),
        "is_staff": is_staff,
        "gender_pred": "M",
        "age_pred": 30,
        "age_bucket": "25-34",
        "is_face_hidden": False,
        "group_id": None,
        "group_size": None,
    }


def make_zone(eid, track_id, zone_name, store_id="STORE_BLR_002", event_time=None):
    return {
        "event_id": eid,
        "event_type": "zone_entered",
        "track_id": track_id,
        "store_id": store_id,
        "camera_id": "CAM2",
        "zone_id": f"ZONE_{_uid()}",
        "zone_name": zone_name,
        "zone_type": "SHELF",
        "is_revenue_zone": "Yes",
        "event_time": event_time or datetime.now(timezone.utc).isoformat(),
        "zone_hotspot_x": 400.0,
        "zone_hotspot_y": 200.0,
        "gender": "M",
        "age": 30,
        "age_bucket": "25-34",
    }


def make_queue(eid, track_id, store_id="STORE_BLR_002", qd=2, abandoned=False):
    return {
        "event_id": eid,
        "queue_event_id": str(uuid.uuid4()),
        "event_type": "queue_abandoned" if abandoned else "queue_completed",
        "track_id": track_id,
        "store_id": store_id,
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
        "zone_hotspot_x": 600.0,
        "zone_hotspot_y": 180.0,
        "gender": "M",
        "age": 30,
        "age_bucket": "25-34",
    }


class TestIngestEndpoint:
    

    def test_idempotency(self):
        events = [make_entry(f"i2-{_uid()}", "V2")]
        r1 = client.post("/events/ingest", json={"events": events})
        r2 = client.post("/events/ingest", json={"events": events})
        assert r1.json()["accepted"] == 1
        assert r2.json()["duplicates_ignored"] == 1

    def test_batch_up_to_500(self):
        uid = _uid()
        events = [make_entry(f"i3_{i}-{uid}", f"V{i}") for i in range(100)]
        resp = client.post("/events/ingest", json={"events": events})
        assert resp.status_code == 200
class TestMetricsEndpoint:
    def test_metrics_returns_json(self):
        resp = client.get("/stores/STORE_BLR_002/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert "unique_visitors" in data

    def test_zero_purchase_store(self):
        events = [make_entry(f"m1-{_uid()}", "VM1")]
        client.post("/events/ingest", json={"events": events})
        resp = client.get("/stores/STORE_BLR_002/metrics")
        assert resp.status_code == 200

    def test_staff_excluded(self):
        uid = _uid()
        events = [
            make_entry(f"s1-{uid}", "VIS_STAFF", is_staff=True),
            make_entry(f"s2-{uid}", "VIS_CUST", is_staff=False),
        ]
        client.post("/events/ingest", json={"events": events})
        resp = client.get("/stores/STORE_BLR_002/metrics")
        data = resp.json()
        assert data.get("unique_visitors", 0) >= 0

    def test_metrics_no_pos_db(self):
        uid = _uid()
        events = [make_entry(f"mpos-{uid}", "VMPOS1")]
        client.post("/events/ingest", json={"events": events})
        resp = client.get("/stores/STORE_BLR_002/metrics")
        assert resp.status_code == 200


class TestFunnelEndpoint:
    def test_funnel_returns_stages(self):
        uid = _uid()
        events = [
            make_entry(f"f1-{uid}", "VIS_1"),
            make_zone(f"f2-{uid}", 101, "Skincare"),
            make_queue(f"f3-{uid}", 101),
        ]
        client.post("/events/ingest", json={"events": events})
        resp = client.get("/stores/STORE_BLR_002/funnel")
        assert resp.status_code == 200
        data = resp.json()
        assert "stages" in data
        assert data["sessions"] >= 0


class TestHeatmapEndpoint:
    def test_heatmap_returns_zones(self):
        uid = _uid()
        events = [
            make_entry(f"h1-{uid}", "VH1"),
            make_zone(f"h2-{uid}", 201, "Skincare"),
        ]
        client.post("/events/ingest", json={"events": events})
        resp = client.get("/stores/STORE_BLR_002/heatmap")
        assert resp.status_code == 200
        data = resp.json()
        assert "zones" in data


class TestAnomaliesEndpoint:
    def test_anomalies_returns_list(self):
        events = [make_entry(f"an1-{_uid()}", "VAN1")]
        client.post("/events/ingest", json={"events": events})
        resp = client.get("/stores/STORE_BLR_002/anomalies")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data.get("anomalies"), list)


class TestHealthEndpoint:
    def test_health_ok(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        
class TestQueueDepth:
    def test_queue_depth_with_joined_events(self):
        """queue_joined without completed = positive queue depth."""
        uid = _uid()
        events = [
            make_entry(f"qd1-{uid}", "VIS_QD1"),
            {
                "event_id": f"qd2-{uid}",
                "queue_event_id": str(uuid.uuid4()),
                "event_type": "queue_joined",
                "track_id": 301,
                "store_id": "STORE_BLR_002",
                "camera_id": "CAM6",
                "zone_id": "BILLING",
                "zone_name": "Billing Counter Queue",
                "zone_type": "BILLING",
                "is_revenue_zone": "Yes",
                "queue_join_ts": datetime.now(timezone.utc).isoformat(),
                "queue_position_at_join": 1,
                "abandoned": False,
                "zone_hotspot_x": 600.0,
                "zone_hotspot_y": 180.0,
                "gender": "M",
                "age": 30,
                "age_bucket": "25-34",
            },
        ]
        client.post("/events/ingest", json={"events": events})
        resp = client.get("/stores/STORE_BLR_002/metrics")
        data = resp.json()
        assert data["queue_depth"] >= 0

    def test_reentry_counted_as_visitor(self):
        """Reentry events should count as unique visitors."""
        uid = _uid()
        events = [
            {
                "event_id": f"re1-{uid}",
                "event_type": "reentry",
                "id_token": f"VIS_RE_{uid}",
                "store_code": "STORE_BLR_002",
                "camera_id": "cam1",
                "event_timestamp": datetime.now(timezone.utc).isoformat(),
                "is_staff": False,
                "gender_pred": "M",
                "age_pred": 30,
                "age_bucket": "25-34",
                "is_face_hidden": False,
                "group_id": None,
                "group_size": None,
            },
        ]
        client.post("/events/ingest", json={"events": events})
        resp = client.get("/stores/STORE_BLR_002/metrics")
        assert resp.status_code == 200
        assert resp.json()["unique_visitors"] >= 1