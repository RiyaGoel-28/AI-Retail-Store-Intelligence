# Technical Choices — Store Intelligence Challenge

Three technical decisions where I deviated from common defaults or AI suggestions, with full reasoning and trade-offs documented. A bonus fourth decision (Staff Detection) is included because it is the single highest-leverage edge case in the brief.

---

## Choice 1 — Detection Model: YOLOv8 Nano

### Options Considered
| Model | Pros | Cons |
|---|---|---|
| **YOLOv8n** ✅ | ~40 ms CPU/frame, mAP-50 ~52% on person, single-file weights | Lower accuracy than larger variants on small / occluded persons |
| YOLOv8m | Better small-object accuracy | ~3× slower on CPU; barely fits the latency budget |
| YOLOv8x | Highest mAP | Too slow for CPU; overkill for clear retail framing |
| RT-DETR | Transformer, strong long-tail | Heavy install; slower on CPU; weaker tooling |
| MediaPipe Pose | Lightweight, Google-backed | Single-person focus; not designed for multi-person retail |

### What AI Suggested
- Claude recommended YOLOv8m as a "balanced middle ground"
- GitHub Copilot suggested YOLOv8n with confidence threshold tuning

### My Decision: **YOLOv8n**
**Reasoning**:
1. Reviewer runs without a GPU. YOLOv8m at `imgsz=1280` blows past the 100ms/frame budget that keeps the pipeline tractable.
2. People in retail CCTV are large, vertical, well-separated objects. YOLOv8n's accuracy is sufficient — the failure cases are crowded shoulder-to-shoulder groups, which a bigger model would also struggle with.
3. The pipeline's bottleneck is the *tracker*, not the detector. Spending the latency budget on a heavier detector doesn't fix re-entry inflation, group-merge errors, or staff classification.

**Trade-off accepted**: marginally lower recall on partially occluded persons. Mitigated by lowering `conf=0.35` (was 0.50) and keeping detections down to 0.25 in the tracker — addresses the spec's "graceful degradation under occlusion" edge case.

**Tuning during validation**:
- Started with `conf=0.50, imgsz=1280, process_every_n=2` → ~6 min/clip on CPU, unusable
- Final: `conf=0.35, imgsz=640, process_every_n=6` → ~1-2 min/clip with no observable loss in event quality

---

## Choice 2 — Event Schema Design: Hybrid Wire Format with Canonical Normalization

### Options Considered
1. **Strict canonical UPPERCASE everywhere** — pipeline emits `ENTRY`, API expects `ENTRY`
2. **Strict lowercase everywhere** — match sample data shape from the brief
3. **Hybrid** ✅ — pipeline emits the sample lowercase shapes, API normalizes on ingest

### What AI Suggested
ChatGPT pushed for canonical UPPERCASE everywhere "for schema purity". Copilot generated lowercase-only handlers based on the sample data.

### My Decision: **Hybrid (normalize on ingest)**
**Reasoning**:
1. The challenge's **sample event JSON uses lowercase** (`entry`, `zone_entered`, `queue_joined`, `queue_completed`, `queue_abandoned`). The **spec table uses UPPERCASE canonical names** (`ENTRY`, `ZONE_ENTER`, `BILLING_QUEUE_JOIN`, `BILLING_QUEUE_COMPLETE`, `BILLING_QUEUE_ABANDON`).
2. Reviewer's evaluation footage may produce events in either shape. A strict-mode API rejects half of valid traffic.
3. `app/schemas.py::_normalize_event_type` maps either shape → canonical UPPERCASE before SQL insert. All downstream metrics work in one form.

**Code shape**:
```python
_EVENT_TYPE_ALIASES = {
    "entry":              "ENTRY",
    "exit":               "EXIT",
    "reentry":            "REENTRY",
    "zone_entered":       "ZONE_ENTER",
    "zone_exited":        "ZONE_EXIT",
    "zone_dwell":         "ZONE_DWELL",
    "queue_joined":       "BILLING_QUEUE_JOIN",
    "queue_completed":    "BILLING_QUEUE_COMPLETE",   # distinct from JOIN
    "queue_abandoned":    "BILLING_QUEUE_ABANDON",
}
```

**Why `BILLING_QUEUE_COMPLETE` is kept distinct**: An earlier iteration collapsed both `queue_joined` and `queue_completed` into `BILLING_QUEUE_JOIN` — that broke the funnel because we couldn't tell "joined the queue" from "actually paid". Splitting them out is what makes the `Entry → Zone → Billing Queue → Purchase` funnel monotonic and correct.

**Trade-off accepted**: per-event normalization runtime cost (negligible — string lookup). Benefit: format flexibility, zero rejected traffic from format-only mismatches.

**What I'd do differently**: pipeline could emit canonical UPPERCASE directly. Kept lowercase to match the spec's sample data verbatim — useful when reviewers diff event JSONL against the brief.

---

## Choice 3 — API Architecture: SQLite + In-Process WebSocket Hub (no Redis, no Postgres, no broker)

### Options Considered
| Architecture | Pros | Cons |
|---|---|---|
| **SQLite + in-process FastAPI WebSocket** ✅ | Zero infra, single `docker compose up`, full state in two files | Single-writer; tops out around 100 events/sec |
| Postgres + Redis pub/sub | Production-scale; horizontal API workers | Three containers; reviewer must wait for healthchecks; dependency hell on Windows |
| Postgres + WebSocket only (no Redis) | Production DB, simple fan-out | Multi-worker fan-out is impossible without a broker |
| Kafka / NATS + Postgres | Industry-grade event stream | Massive overkill for a 5-store evaluation; reviewer abandons |

### What AI Suggested
Claude pitched **Postgres + Redis pub/sub** as the "production-correct" answer and described how to wire it. Copilot defaulted to SQLite. ChatGPT offered both and asked me to clarify the deployment target.

### My Decision: **SQLite × 2 + in-process WebSocket hub**

**Reasoning**:

1. **The acceptance gate is `docker compose up` on a clean machine.** Every additional service is one more thing that can fail to start, port-conflict, or take 30 s on a healthcheck. A reviewer who hits a Redis ECONNREFUSED on first boot scores me as "doesn't run" — irrespective of code quality. SQLite keeps the cold-start path to a single container.

2. **Two SQLite files instead of one** — separation of concerns:
   - `data/events.db` — high-write event store, rebuilt every pipeline run
   - `data/store_intelligence.db` — read-mostly POS reference data, loaded once from `pos_transactions.csv`

   This lets me `rm events.db && rerun` for a clean idempotent reset without losing POS context. Two tables in one DB would also work; two files made dev iteration faster.

3. **In-process WebSocket hub (`app/ws.py`)** — single broadcast set kept in memory, fanned out from `/events/ingest`. With one API container the broker is unnecessary. Once you scale to 2+ workers, this breaks (each worker only sees its own ingests) — at which point I'd swap in Redis pub/sub. Documented in DESIGN.md §9.

4. **Single-writer SQLite is acceptable for the workload**: the brief mentions 40 stores. At realistic retail density (~1 event/sec/store at peak), that's ~40 events/sec — well below SQLite's WAL-mode write ceiling (~5,000/sec on commodity hardware). The choice only breaks at the next order of magnitude.

**Trade-off accepted**:
- **No horizontal scaling.** A single API process serves all clients. Acknowledged limitation; I'd address by switching to Postgres + Redis pub/sub at the first production scale-out, not before.
- **No replication / no HA.** SQLite with `PRAGMA journal_mode=WAL` survives crashes but not disk loss. Out of scope for an evaluation submission; trivially solved by mounting a backed-up volume in production.
- **WebSocket clients must reconnect on API restart.** Acceptable for a dashboard; would be unacceptable for paid customer traffic.

**What would make me change this decision**:
- More than one API replica → Redis pub/sub becomes mandatory
- Multi-region deployment → Postgres + read replicas for `/metrics` etc.
- Event volume crossing 1k/sec sustained → Postgres + a queue (Kafka or NATS) ahead of ingest
- A hard SLA on API restart with no event loss → durable broker required

**Why I prefer this default for a 24-hour build**: it makes every other thing in the system *testable* without infra. Pytest can hit the real DB with no fixtures; tests that exercise WebSocket fan-out (`tests/test_ws_publish.py`) run against the same code path that production uses. A reviewer can clone-and-run in 60 seconds. That tight feedback loop is worth more in this context than horizontal-scale-readiness.

---

## Choice 4 (Bonus) — Staff Detection: Behavioural Heuristic, Calibrated for Short Clips

> Included as a fourth choice because the brief explicitly highlights staff exclusion as a scored edge case (Part A) and the AI-vs-human reasoning is unusually concrete here.

### Options Considered
| Approach | Accuracy | Generalisation | Effort |
|---|---|---|---|
| Uniform HSV color match | High on tuned camera | Fails on different uniforms / low light | Low |
| Face recognition (reference photos) | High for visible faces | Fails on small / back-facing crops; needs photos per store | Medium |
| **Behavioural heuristic** ✅ | Medium-high after tuning | Camera-agnostic, store-agnostic | Low |

### What AI Suggested
Claude initially proposed HSV uniform matching ("Purplle uniforms are dark purple"). I rejected after considering generalisation.

### My Decision: **Behavioural heuristic with AND-of-strong-signals, clip-tuned**

A track is flagged as staff iff **all three** of the following hold:

| Constant (in `pipeline/detect.py`) | Value | Meaning |
|---|---|---|
| `STAFF_MIN_DISTINCT_ZONES` | **3** | Visited at least 3 different zones (customer typically browses 1-2) |
| `STAFF_MIN_FRAMES` | **20** | Has been seen in at least 20 *processed* detection frames |
| `STAFF_MIN_SPAN_FRAMES` | **600** | Track lifetime spans at least 600 *source* frames (~40 s @ 15 fps) |

These thresholds are deliberately calibrated for the **20-minute challenge clips** and are the result of three iterations:

| Iteration | Rule | Result |
|---|---|---|
| v1 | `≥3 zones OR ≥2 min` (OR, loose) | ~40% of customers mis-flagged as staff |
| v2 | `≥5 zones AND ≥5 min` (AND, "production-grade") | ~0% staff flagged — too strict, no track survives 5 min @ 15 fps with frame-skipping in a 20-min clip |
| v3 (current) | `≥3 zones AND ≥20 frames AND ≥600 source frames` (AND, clip-tuned) | ~21% staff fraction — matches realistic retail density |

**Why all three signals (AND, not OR, not just two)**:
- v1 (OR) was too permissive — a customer who browses 3 sections fires the zone signal alone
- A "≥X zones AND ≥Y minutes" rule still produces false-positives on customers who linger; adding the **detection-frame count** as a third signal filters out ghost tracks (intermittent re-detections of the same person spread across the clip) which would otherwise satisfy a pure span-of-time check

**Why this generalises**:
- Doesn't depend on uniform color → works on reviewer's footage with any uniform
- Doesn't depend on face quality → works on small or back-facing detections
- Doesn't need reference photos → no per-store setup

**Sticky flag + retroactive backfill**:
The flag is sticky — once a track crosses the threshold, all of its events (including the early `entry` and `zone_entered` rows emitted *before* the threshold was crossed) are flagged `is_staff=true`. The API ingest layer (`app/ingestion.py`) does a second pass: if any event in the batch reports staff for a given visitor, all earlier rows are retroactively backfilled. Without this, staff would have leaked into the customer pool on every clip's first 30 seconds.

**Trade-offs accepted**:
- Needs ~40 s of in-store observation before reliable
- Misses staff with very brief on-camera appearances (e.g. someone covering a 30 s phone call)
- False negatives inflate visitor count by ~5-10 % → acceptable for a heuristic; production would add a uniform/face confirmation signal alongside

**Production scaling**:
The constants are module-level on purpose. For a real 8-hour-shift deployment they should scale up — `STAFF_MIN_DISTINCT_ZONES=5`, `STAFF_MIN_SPAN_FRAMES=4500` (~5 min @ 15 fps) or higher. Tunable as a config change, not a code change.

**What I would change with more data**: Collect labelled samples from 1-2 stores, train a binary classifier on (zones, dwell, hour-of-day, motion entropy, track length) — would likely push accuracy from heuristic ~90 % to learned ~96-98 %. Out of scope here without ground truth.

---

## Summary

| Decision | Chose | Rejected | Key Reason |
|---|---|---|---|
| Detection model | YOLOv8n | YOLOv8m, RT-DETR, MediaPipe | CPU latency budget; tracker is the bottleneck, not detector |
| Event schema | Hybrid (lowercase pipeline, UPPERCASE API) with distinct `BILLING_QUEUE_COMPLETE` | Strict either way; collapsed JOIN/COMPLETE | Format flexibility; funnel needs JOIN ≠ COMPLETE to be monotonic |
| **API architecture** | **SQLite × 2 + in-process WebSocket** | **Postgres + Redis pub/sub; Kafka/NATS** | **Acceptance gate is `docker compose up`; every extra service is reviewer friction** |
| Staff detection (bonus) | Behavioural heuristic — clip-tuned (3 zones / 20 frames / 600 span) with sticky flag + retroactive backfill | Uniform HSV, face recognition; "production-grade 5/5min" rule | Generalisation > peak accuracy; thresholds must be sized to observation window |