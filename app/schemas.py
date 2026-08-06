"""
Pydantic schemas for the Store Intelligence API.
Accepts the actual challenge event formats: entry/exit, zone, and queue events.
"""
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
# Map sample-format (lowercase) event types → canonical (uppercase) spec types.
# Canonical types: ENTRY, EXIT, ZONE_ENTER, ZONE_EXIT, ZONE_DWELL,
#                  BILLING_QUEUE_JOIN, BILLING_QUEUE_ABANDON, REENTRY
_EVENT_TYPE_ALIASES = {
    "entry":              "ENTRY",
    "exit":               "EXIT",
    "reentry":            "REENTRY",
    "zone_entered":       "ZONE_ENTER",
    "zone_exited":        "ZONE_EXIT",
    "zone_dwell":         "ZONE_DWELL",
    "queue_joined":       "BILLING_QUEUE_JOIN",
    "queue_completed":    "BILLING_QUEUE_COMPLETE",     
}

CANONICAL_EVENT_TYPES = {
    "ENTRY", "EXIT", "REENTRY",
    "ZONE_ENTER", "ZONE_EXIT", "ZONE_DWELL",
    "BILLING_QUEUE_COMPLETE", "BILLING_QUEUE_ABANDON",
}


def _normalize_event_type(raw: str) -> str:
    """Convert any event_type to canonical UPPERCASE form.
    Already-canonical values pass through unchanged."""
    if not raw:
        return ""
    upper = raw.strip().upper()
    if upper in CANONICAL_EVENT_TYPES:
        return upper
    return _EVENT_TYPE_ALIASES.get(raw.strip().lower(), upper)

class Event(BaseModel):
    """Flexible event model that accepts all 3 event shapes from the challenge data."""
    # Common
    event_type: str

    # Entry/Exit fields
    id_token: Optional[str] = None
    store_code: Optional[str] = None
    event_timestamp: Optional[str] = None
    is_staff: Optional[bool] = False
    gender_pred: Optional[str] = None
    age_pred: Optional[int] = None
    is_face_hidden: Optional[bool] = None
    group_id: Optional[str] = None
    group_size: Optional[int] = None

    # Zone fields
    track_id: Optional[Any] = None
    store_id: Optional[str] = None
    camera_id: Optional[str] = None
    zone_id: Optional[str] = None
    zone_name: Optional[str] = None
    zone_type: Optional[str] = None
    is_revenue_zone: Optional[str] = None
    event_time: Optional[str] = None
    zone_hotspot_x: Optional[float] = None
    zone_hotspot_y: Optional[float] = None
    gender: Optional[str] = None
    age: Optional[int] = None
    age_bucket: Optional[str] = None

    # Queue/Billing fields
    queue_event_id: Optional[str] = None
    queue_join_ts: Optional[str] = None
    queue_served_ts: Optional[str] = None
    queue_exit_ts: Optional[str] = None
    wait_seconds: Optional[int] = None
    queue_position_at_join: Optional[int] = None
    abandoned: Optional[bool] = None

    # Legacy / internal fields (for backward compat)
    event_id: Optional[str] = None
    visitor_id: Optional[str] = None
    timestamp: Optional[str] = None
    dwell_ms: Optional[int] = 0
    confidence: Optional[float] = Field(default=1.0, ge=0.0, le=1.0)
    metadata: Optional[Dict[str, Any]] = None

    model_config = {"extra": "allow"}

    def get_store_id(self) -> str:
        """Prefer canonical `store_id`; fall back to sample `store_code`."""
        return self.store_id or self.store_code or ""

    def get_visitor_id(self) -> str:
        """Prefer canonical `visitor_id`; fall back to sample `id_token` / `track_id`."""
        if self.visitor_id:
            return self.visitor_id
        if self.id_token:
            return self.id_token
        if self.track_id is not None:
            return str(self.track_id)
        return ""

    def get_timestamp(self) -> str:
        """Prefer canonical `timestamp`; fall back to sample `event_timestamp` / `event_time` / `queue_join_ts`."""
        return (
            self.timestamp
            or self.event_timestamp
            or self.event_time
            or self.queue_join_ts
            or ""
        )

    def get_event_id(self) -> str:
        """Prefer canonical `event_id`; fall back to sample `queue_event_id`; else generate uuid."""
        import uuid
        return self.event_id or self.queue_event_id or str(uuid.uuid4())

    def get_camera_id(self) -> str:
        return self.camera_id or "UNKNOWN"

    def get_is_staff(self) -> bool:
        return bool(self.is_staff)

    def get_event_type(self) -> str:
        """Normalize event_type to canonical UPPERCASE form for downstream comparison."""
        return _normalize_event_type(self.event_type)

class IngestRequest(BaseModel):
    events: List[Event]


class IngestResponse(BaseModel):
    status: str
    accepted: int
    duplicates_ignored: int
    rejected: List[dict] = []
    inserted_ids: List[str] = []