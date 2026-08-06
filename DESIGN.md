# Store Intelligence API — Design Document

## 1. System Architecture

End-to-end pipeline that turns raw retail CCTV into live operational intelligence:

```
CCTV Footage ─► Detection Pipeline ─► Event Stream ─► REST API + WebSocket ─► Live Dashboard
                  (YOLOv8 + Tracker)    (JSONL/POST)     (FastAPI + SQLite)     (HTML + WS)
```

### 1.1 Detection Pipeline (offline / per-clip)
- **Person detector**: YOLOv8 Nano (`yolov8n.pt`) — `imgsz=640`, `conf=0.35`, person class only
- **Tracker**: Lightweight distance-based associator with re-ID window (no DeepSORT, no embeddings)
- **Direction inference**: For fixed-mount cameras, first detection of a track = `entry`, track timeout = `exit`. Optical-flow direction tracking was considered but rejected for this challenge — with cameras pointed straight at the threshold, every track's lifetime *is* the entry→exit traversal, and adding flow estimation would just add latency without changing the events emitted. For wider angles or thresholdless doorways, vector-direction inference would be required.
- **Zone classifier**: Maps bounding-box centers to named zones from `data/store_layout.json`
- **Event emitter**: Produces the **8 spec-mandated event types** plus `BILLING_QUEUE_COMPLETE` (the queue analogue of `EXIT` — see §3.5 for why this is necessary for funnel correctness). Wire format is the lowercase form shown in `data/sample_events.jsonl`; the API normalizes to canonical UPPERCASE on ingest.
- **Submission deliverable**: The pipeline output for the provided CCTV clips is committed at **`data/processed/all_events.jsonl`** (combined, 893 events) and per-camera JSONL files alongside it. Reviewers can diff this directly against the spec schema or replay it through the API via `pipeline/emit.py`.

### 1.2 Intelligence API
- **Framework**: FastAPI + Pydantic v2
- **Storage**: Two SQLite files (separation of concerns, see §3.4)
  - `data/events.db` — visitor / zone / queue events (high write rate)
  - `data/store_intelligence.db` — POS transactions, loaded once from `data/pos_transactions.csv` (read-mostly reference data)
- **Mandatory endpoints** (per spec): `/events/ingest`, `/stores/{id}/metrics|funnel|heatmap|anomalies`, `/health`
- **Bonus endpoints**: `/ws` (real-time fan-out for the dashboard) and `/stores/{id}/staff-stats` (a *diagnostic* endpoint surfacing the customer-vs-staff exclusion breakdown — useful for reviewers verifying that the staff heuristic isn't silently over-flagging; not part of the customer-metrics contract)

### 1.3 Live Dashboard
- Single-file HTML + JS, served by FastAPI
- Subscribes to `/ws` for live event stream
- Re-pulls metrics on each push for store cards / funnel / queue depth

### 1.4 Containerisation
- `docker compose up` starts the API + dashboard on port 8000
- Detection pipeline runs on the host (needs YOLOv8 weights + OpenCV) — keeps the API image small

---

## 2. Data Flow

```
Frame ─► YOLOv8 (person detection)
       ─► Distance-based tracker (assigns visitor_id, handles re-entry)
       ─► Zone classifier (current zone per track)
       ─► Event emitter (entry / exit / zone_* / queue_*)
       ─► JSONL → POST /events/ingest
       ─► Pydantic validation + canonical UPPERCASE normalization
       ─► SQLite insert (idempotent on event_id)
       ─► WebSocket broadcast → dashboard
       ─► GET /stores/{id}/metrics aggregates on demand
```

---

## 3. Key Design Decisions

### 3.1 YOLOv8 Nano over larger variants
Person detection in retail CCTV is comparatively easy — large bounding boxes, clear vertical silhouettes. The bottleneck is **CPU latency on host without GPU**. YOLOv8n at `imgsz=640` runs ~30-60 ms/frame on CPU; with frame skipping (`process_every_n=6`) we get ~5 effective fps which is plenty for tracking adults walking through a store.

### 3.2 Distance-based tracking over DeepSORT
DeepSORT's appearance embeddings add a second neural net (~150-200 ms/frame). For **fixed-mount cameras** with predictable motion, a 200-px IoU/centroid match handles the common case. We accept slightly weaker re-association across long occlusions; we compensate with the explicit re-entry window (§3.6).

### 3.3 SQLite over PostgreSQL
- ✅ Zero-infra → reviewer can `docker compose up` with no DB setup
- ✅ Single-file portability for submission
- ⚠️ Production scaling (40+ stores, real-time ingest): would migrate to Postgres + pgbouncer + Redis read cache. SQLite write-lock contention starts hurting around 100+ events/sec.

### 3.4 Two SQLite files (one per concern)
- `events.db` is a **streaming write store** — rebuilt every pipeline run
- `store_intelligence.db` holds POS reference data — loaded once from CSV
- Allows clean idempotent resets (`rm events.db && rerun`) without losing POS context
- Could consolidate to one file with two tables in production; kept separate during dev for faster iteration

### 3.5 Wire format: lowercase events on the pipeline, canonical UPPERCASE in the API
The pipeline emits `entry`, `zone_entered`, `queue_joined`, `queue_completed`, `queue_abandoned`, etc. — matching the sample data shapes in the challenge. `app/schemas.py::_normalize_event_type` maps these to the canonical spec types on ingest:

| Pipeline (lowercase) | Canonical (UPPERCASE) | In spec catalogue? |
|---|---|---|
| `entry` | `ENTRY` | ✅ |
| `exit` | `EXIT` | ✅ |
| `reentry` | `REENTRY` | ✅ |
| `zone_entered` | `ZONE_ENTER` | ✅ |
| `zone_exited` | `ZONE_EXIT` | ✅ |
| `zone_dwell` | `ZONE_DWELL` | ✅ |
| `queue_joined` | `BILLING_QUEUE_JOIN` | ✅ |
| `queue_abandoned` | `BILLING_QUEUE_ABANDON` | ✅ |
| `queue_completed` | `BILLING_QUEUE_COMPLETE` | ➕ added (not in spec catalogue) |

**Why `BILLING_QUEUE_COMPLETE` is kept distinct**: The spec catalogue lists 8 event types and does *not* include a queue-completion event. We added it because the funnel `Entry → Zone → Billing Queue → Purchase` requires distinguishing "joined the queue" from "actually paid" — without `BILLING_QUEUE_COMPLETE`, every queue join would look identical to a queue abandon, and the conversion-rate column of the funnel would be uncomputable from events alone (we'd be entirely dependent on POS correlation, which has a ±5-min window and silently misses queue-only conversions). The system accepts both the lowercase sample format and the canonical UPPERCASE format transparently.

### 3.6 Re-entry handling (deliberate suppression of inflation)
Re-entry inflation is a known CCTV vendor problem. The tracker:
1. Holds **exited** tracks in a 30-second re-association window (`reentry_window_frames = 450 @ 15 fps`)
2. On a new detection within that window with a similar last position, emits `REENTRY` reusing the original `visitor_id` (no double-count)
3. After 30s, the track is purged and any new detection starts fresh — **deliberately conservative** to avoid false merges across long absences

### 3.7 Zone-flicker debounce
Customers near zone borders can trigger rapid `zone_entered/zone_exited` cycles. Two debounces:
- `zone_change_cooldown = 8 frames` (~0.5 s) suppresses sub-second oscillation
- `queue_cooldown_frames = 150` (~10 s) prevents `queue_joined` immediately after `queue_completed` for the same track

### 3.8 Identity unification across event types
Every emitted event — `entry`, `exit`, `reentry`, `zone_*`, `queue_*` — carries the same `id_token` (= `visitor_id`). This is what allows the funnel to compute strict-subset stages (`Entry ⊇ Zone Visit ⊇ Billing Queue ⊇ Purchase`) without phantom "ghost" sessions appearing only at the zone or queue stage. Earlier iterations omitted `id_token` on zone events, which inflated Zone Visit to >100% of entries; the unification fix restored monotonicity.

---

## 4. Edge Case Handling

| Edge Case | How it's handled | Trade-off |
|---|---|---|
| **Group entry** (2-4 people through one door) | YOLO emits one box per person; tracker assigns separate IDs; 200-px match radius is wide enough for walking pace, tight enough to keep close-spaced people separate | YOLO occasionally merges shoulder-to-shoulder pairs into one box → undercount in dense groups. Documented; would address with stronger ReID in production. |
| **Staff movement** (must be excluded from customer metrics) | Behavioural heuristic, calibrated for the 20-min challenge clips: a track is staff iff it visits **≥3 distinct zones** AND has been observed in **≥20 detection frames** spanning **≥600 source frames (~40 s @ 15 fps)**. See §5 for thresholds, calibration, and production scaling. | Heuristic needs ~40 s of in-store observation; very brief staff appearances may be missed. Validated false-positive rate ~10%; was ~40% before tightening. |
| **Re-entry inflation** | 30 s re-association window, spatial match, `REENTRY` event reuses original visitor_id | Returns >30 s after exit are counted as new visitors — conservative bias, prevents false merges |
| **Partial occlusion** | YOLO `conf=0.35` (relaxed from default); detections kept down to 0.25; `is_face_hidden=True` flagged when confidence < 0.6 | Lower-conf detections produce more candidate tracks → marginally noisier funnel for crowded frames; downstream metrics unaffected because they de-duplicate by visitor_id |
| **Billing queue buildup** | `queue_joined` on BILLING-zone enter, `queue_completed` on exit, `queue_abandoned` if track times out while still `in_billing`. Live `queue_depth` = count of tracks with `in_billing=True`. | Doesn't yet expose peak-depth-over-time; current depth is shown live |
| **Empty store / no events today** | Metrics endpoint returns valid response with zeros; no crashes; falls back to all-time data when today is empty (documented in metric response) | Reviewer should know `as_of` field tells them which window the figures are from |
| **Camera overlap** | The challenge layout assigns each physical camera to a *distinct* role — entry, main floor, billing — and zone polygons in `store_layout.json` are camera-scoped. This means the same physical person seen briefly across the entry/floor overlap is counted at most once per visitor_id by the metrics aggregations (which de-dupe on `visitor_id`). True cross-camera ReID is not implemented; if reviewer footage has overlapping fields with the same visitor moving across them, the same person could be assigned two IDs (one per camera). Documented as a known limitation in §9. | No cross-camera embedding model; relies on physical camera-role separation in the layout |
| **Stale feed** | `/health` reports `STALE_FEED` per store if last event > 10 min ago | Threshold is hardcoded; production would make it per-store configurable |
| **Duplicate ingest** | `event_id` is the SQLite primary key → second POST is a no-op; response reports `duplicates_ignored` | Producer must supply event_id; the pipeline does this with UUIDs |
| **DB unavailable** | Endpoints return HTTP 503 with structured JSON, no stack traces | No retry / circuit breaker — relying on container restart policy |

---

## 5. Staff Detection — Multi-Signal Reasoning

The challenge requires staff to be excluded from customer metrics. We considered three approaches:

| Approach | Pros | Cons | Decision |
|---|---|---|---|
| Uniform color matching (HSV) | Direct visual signal | Per-camera HSV calibration; fails in low light & backlit shots; doesn't generalize to evaluation footage with different uniforms | ❌ Rejected |
| Face/ReID with reference photos | High accuracy when faces visible | Needs reference photos; CCTV faces often <80 px; doesn't generalize when reviewer uses footage of different stores/staff | ❌ Rejected |
| **Behavioural heuristic** (chosen) | Camera-agnostic; works regardless of uniform; needs no per-store calibration | Needs an observation window before classification; misses very brief staff appearances | ✅ Chosen |

### 5.1 Calibrated thresholds (current code)

A track is flagged as staff iff **all three** of the following hold (AND of strong signals):

| Constant (in `pipeline/detect.py`) | Value | Meaning |
|---|---|---|
| `STAFF_MIN_DISTINCT_ZONES` | **3** | Visited at least 3 different zones (typical customer browses 1–2) |
| `STAFF_MIN_FRAMES` | **20** | Has been seen in at least 20 *processed* detection frames |
| `STAFF_MIN_SPAN_FRAMES` | **600** | Track lifetime spans at least 600 *source* frames (~40 s @ 15 fps) |

These thresholds are deliberately calibrated for the **20-minute challenge clips**. With only ~20 minutes of footage per camera, a stricter "≥5 zones AND ≥5 minutes" rule (which would suit a real production deployment with 8-hour shifts) almost never fires — in validation it produced 0 staff flags out of 60+ tracks because no track survived 5 minutes of CCTV at 15 fps with frame-skipping.

### 5.2 Calibration history

| Iteration | Rule | Result on challenge clips |
|---|---|---|
| v1 | `≥3 zones OR ≥2 min` (OR, loose) | ~40% of customers mis-flagged as staff |
| v2 | `≥5 zones AND ≥5 min` (AND, production-grade) | ~0% staff detected — too strict for short clips |
| v3 (current) | `≥3 zones AND ≥20 frames AND ≥600 source frames` (AND, clip-tuned) | ~21% staff fraction across both stores; matches realistic retail staff density |

### 5.3 Sticky flag + retroactive backfill

The flag is **sticky** — once a track crosses the threshold, all of its events (including those emitted before the threshold was crossed) are flagged `is_staff=true`. The API's ingest layer (`app/ingestion.py`) performs a second pass: if any event in the batch reports `is_staff=true` for a given visitor, all earlier rows for that visitor are retroactively backfilled. This eliminates the late-firing-heuristic edge case where the first few `entry`/`zone_entered` rows for a staff member would otherwise be wrongly attributed to the customer pool.

### 5.4 Trade-offs explicitly accepted

- **False positive** (customer flagged as staff): excluded from `unique_visitors` but still tracked for funnel/heatmap → degrades gracefully
- **False negative** (staff flagged as customer): inflates visitor count by ~5-10 % → acceptable for a heuristic; production would add a uniform / face confirmation signal alongside

### 5.5 Production scaling

The constants are module-level on purpose — for a real 8-hour-shift deployment they should scale up linearly: `STAFF_MIN_DISTINCT_ZONES=5`, `STAFF_MIN_SPAN_FRAMES=4500` (~5 min @ 15 fps) or higher. They were left tunable rather than hardcoded inside the class so a reviewer / ops engineer can adjust without touching tracker internals.

---

## 6. AI-Assisted Decisions

This section documents three places where an LLM materially shaped the design — and explicitly states whether I agreed with the AI suggestion or overrode it after evaluation. Per the spec, the focus is on intentional use, not volume.

### Decision 1 — Event schema: flat vs polymorphic vs hybrid
- **AI suggested**: ChatGPT proposed polymorphic Pydantic models per event type for "type safety". Claude proposed a hybrid with a `metadata` object.
- **Verdict: Partially overrode** — kept Claude's hybrid shape but rejected ChatGPT's per-type polymorphism.
- **What I did**: Hybrid + `extra: "allow"`. The challenge sample data has 3 different event shapes (entry, zone, queue), each with their own native fields. A single flexible model accepts all three, normalizes lowercase types to canonical UPPERCASE on ingest, and stores the full original payload as JSON for replay/debug.
- **Why**: Wire-format flexibility was more important than compile-time type strictness. Per-type polymorphism would have required the producer (the pipeline) to know which model to instantiate, which is fragile when the pipeline output is JSONL written by a separate process. The cost is per-event runtime validation; the benefit is the API accepts both sample-format and spec-format transparently.

### Decision 2 — Staff detection: uniform vs face vs heuristic
- **AI suggested**: Claude initially proposed uniform color matching on the torso region.
- **Verdict: Overrode the suggestion entirely.**
- **What I did**: Rejected uniform/face approaches (see §5) and used the behavioural heuristic. Tightened thresholds during validation when initial settings produced 40 % false positives, then re-loosened from "production-grade 5/5min" to "clip-tuned 3/40s" once the production rule produced zero detections on 20-min clips.
- **Why**: Generalisation to evaluation footage matters more than peak accuracy on our own clips, but the thresholds must be sized to the observation window. Module-level constants make production scaling a config change, not a code change. Uniform colour matching would have been brittle on the reviewer's footage (different store, different uniform, possibly different camera white-balance).

### Decision 3 — Zone classification: VLM vs coordinate-based
- **AI suggested**: GPT-4V proposed using a vision-language model to identify zones from frames at runtime ("ask Claude Vision: which department is this person standing in?").
- **Verdict: Overrode for runtime, kept for offline authoring.**
- **What I did**: Rejected for primary detection. `store_layout.json` already has explicit zone polygons → coordinate-based check is faster, deterministic, and doesn't need an extra model in the container.
- **Where the VLM still helps**: Initial layout authoring — using GPT-4V on a still frame to *generate the layout JSON*. That's a one-time offline step, not a runtime cost. Documenting this distinction was itself an LLM-shaped decision: when ChatGPT was asked "should we use a VLM here?", the useful answer turned out to be "yes, but at design time, not request time".

---

## 7. Production Considerations

- **Structured logging**: Every request logged with method, path, status, latency, trace-id
- **Idempotent ingest**: `POST /events/ingest` safe to call twice (PK constraint on `event_id`)
- **Graceful degradation**: DB unavailable → 503 + structured body, no stack traces leaked
- **Health monitoring**: `/health` returns per-store `STALE_FEED` flag if no events in the last 10 minutes
- **Schema validation**: Pydantic rejects malformed events with field-level error detail; rejected events are surfaced in the ingest response (not silently dropped)
- **Diagnostic surface**: `/stores/{id}/staff-stats` exposes the customer-vs-staff exclusion split as an observability hook, so an operator can spot if the heuristic starts over-flagging in production without redeploying

---

## 8. Performance Characteristics (measured on dev laptop, CPU-only)

| Workload | Latency | Notes |
|---|---|---|
| YOLOv8n inference | ~30-60 ms/frame at imgsz=640 | CPU; would be <10 ms on any modest GPU |
| Pipeline end-to-end | ~1-2 min per 4000-frame clip | with `process_every_n=6` |
| `POST /events/ingest` (batch of 200) | <300 ms | dominated by SQLite commit |
| `GET /stores/{id}/metrics` | ~100-400 ms | aggregates over today's events; would add Redis cache + materialized counters in production |
| `GET /health` | <50 ms | single GROUP BY query |

---

## 9. Known Limitations

1. **No GPU path** — CPU-only; deliberate, given submission portability requirement
2. **Single-camera tracking only** — no cross-camera ReID. Mitigated by camera-role separation in `store_layout.json` (entry / floor / billing cameras cover distinct areas), but if reviewer footage has overlapping fields-of-view with the same person walking across them, the same person could be assigned two IDs.
3. **Heuristic staff detection** — needs an observation window; not infallible (see §5 trade-offs). Thresholds are clip-tuned; production deployments should re-scale per §5.5.
4. **Frame-skip aliasing** — `process_every_n=6` means ~0.2 s blind spots; fast events (a customer crossing a zone in 0.1 s) may be missed
5. **POS correlation is timestamp-based, not visitor-based** — no face/payment-card linkage between visitor and transaction; we infer purchase by `queue_completed` co-occurring with a POS row near the same time
6. **Single API replica** — in-process WebSocket fan-out doesn't survive horizontal scale-out; would swap in Redis pub/sub at the first `replicas: 2` config change (see CHOICES.md §3)