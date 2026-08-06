"""
# PROMPT: Generate pytest tests for idempotent event ingestion using actual
# challenge format — verify duplicate id_token entry events are ignored.
# CHANGES MADE: Updated to actual event format with id_token, store_code,
# event_timestamp. Idempotency uses generated event_id from UUID.
"""
import uuid
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def _make_events():
    uid = uuid.uuid4().hex[:5]
    return [
        {
            "event_id": f"idempo-1-{uid}",
            "event_type": "entry",
            "id_token": f"ID_{uid}_1",
            "store_code": "STORE_IDEMP_001",
            "camera_id": "cam1",
            "event_timestamp": "2026-03-04T09:00:00.000000",
            "is_staff": False,
            "gender_pred": "M",
            "age_pred": 30,
            "age_bucket": "25-34",
            "is_face_hidden": False,
            "group_id": None,
            "group_size": None,
        },
        {
            "event_id": f"idempo-2-{uid}",
            "event_type": "entry",
            "id_token": f"ID_{uid}_2",
            "store_code": "STORE_IDEMP_001",
            "camera_id": "cam1",
            "event_timestamp": "2026-03-04T09:01:00.000000",
            "is_staff": False,
            "gender_pred": "F",
            "age_pred": 25,
            "age_bucket": "25-34",
            "is_face_hidden": False,
            "group_id": None,
            "group_size": None,
        },
    ]


def test_idempotent_ingest():
    events = _make_events()
    r1 = client.post("/events/ingest", json={"events": events})
    assert r1.status_code == 200
    b1 = r1.json()
    assert b1["accepted"] == 2
    assert b1["duplicates_ignored"] == 0

    r2 = client.post("/events/ingest", json={"events": events})
    assert r2.status_code == 200
    b2 = r2.json()
    assert b2["accepted"] == 0
    assert b2["duplicates_ignored"] == 2


def test_partial_rejection():
    events = _make_events()
    bad = events + [{"store_code": "STORE_IDEMP_001"}]  # missing event_type
    r = client.post("/events/ingest", json={"events": bad})
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] == 2
    assert len(body["rejected"]) == 1