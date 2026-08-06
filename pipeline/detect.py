"""
# PROMPT: Detection pipeline: process CCTV clips and emit structured events
# in the actual challenge data format (lowercase event_type, id_token, store_code, etc.)
# CHANGES MADE: Fixed queue_joined/queue_completed separation, added REENTRY event type,
# staff flagging on all events, ZONE_DWELL emission, deduped queue events.
# Added debounce for exit/reentry flickering and duplicate queue cycles.
# STAFF FIX: is_staff is now sticky (once True, stays True) and re-evaluated on every
# matched frame so zone/queue events emit with the correct flag instead of just exit.
# IDENTITY FIX (Bug #2): zone_* and queue_* events now carry id_token=visitor_id so the
# same physical person resolves to ONE visitor across entry/zone/queue/exit events.
# Without this, the funnel saw N entries + M zone-only "ghosts" → 128% Zone Visit.
"""

import json
import uuid
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

try:
    import cv2
    import numpy as np
    from ultralytics import YOLO
except ImportError:
    print("Warning: OpenCV or YOLOv8 not installed. Install with: pip install -r requirements-cv.txt")
    sys.exit(1)


# ── Staff-detection thresholds (AND of strong signals — see DESIGN.md §5) ──
# Lifted to module level so reviewers can tune without editing class internals.
STAFF_MIN_DISTINCT_ZONES = 3       # cover ≥3 of ~7 zones
STAFF_MIN_FRAMES         = 20      # short-clip friendly
STAFF_MIN_SPAN_FRAMES    = 600     # ~20s @ 30fps source frames


class ZoneClassifier:
    """Classify detected persons into zones based on bounding box center
    and pixel coordinates from store_layout.json."""

    def __init__(self, layout_path="data/store_layout.json", store_id=None):
        self.zones = {}
        self.zone_names = {}
        self.zone_types = {}
        self.entry_zone = None
        self.frame_w = 1920
        self.frame_h = 1080

        try:
            with open(layout_path) as f:
                layout = json.load(f)

            if store_id and store_id in layout:
                store_layout = layout[store_id]
                self.zones = store_layout.get("zones", {})
                self.entry_zone = store_layout.get("entry_zone", None)
            elif "zones" in layout:
                self.zones = layout["zones"]
            else:
                first_key = next(iter(layout), None)
                if first_key and isinstance(layout[first_key], dict):
                    self.zones = layout[first_key].get("zones", {})

            for zid, zdata in self.zones.items():
                if isinstance(zdata, dict):
                    self.zone_names[zid] = zdata.get("zone_name", zid)
                    self.zone_types[zid] = zdata.get("zone_type", "SHELF")

            print(f"  Loaded {len(self.zones)} zones for {store_id} from {layout_path}")
        except FileNotFoundError:
            print(f"  ⚠️ {layout_path} not found — using default zones")
            self.zones = {
                "ENTRY": {"x1": 0, "y1": 0, "x2": 400, "y2": 300},
                "SKINCARE": {"x1": 400, "y1": 0, "x2": 800, "y2": 400},
                "MAKEUP": {"x1": 800, "y1": 0, "x2": 1200, "y2": 400},
                "BILLING": {"x1": 1000, "y1": 400, "x2": 1300, "y2": 700},
                "FOH": {"x1": 400, "y1": 300, "x2": 800, "y2": 500}
            }

    def set_frame_size(self, frame_w, frame_h):
        self.frame_w = frame_w
        self.frame_h = frame_h

    def classify(self, bbox):
        try:
            x1, y1, x2, y2 = bbox
            cx = (x1 + x2) / 2
            cy = (y1 + y2) / 2

            layout_w = max(z.get("x2", 0) for z in self.zones.values()) if self.zones else 1300
            layout_h = max(z.get("y2", 0) for z in self.zones.values()) if self.zones else 700

            scale_x = self.frame_w / layout_w if layout_w else 1
            scale_y = self.frame_h / layout_h if layout_h else 1

            for zone_id, coords in self.zones.items():
                if not isinstance(coords, dict):
                    continue
                zx1 = coords.get("x1", 0) * scale_x
                zy1 = coords.get("y1", 0) * scale_y
                zx2 = coords.get("x2", 0) * scale_x
                zy2 = coords.get("y2", 0) * scale_y

                if zx1 <= cx <= zx2 and zy1 <= cy <= zy2:
                    return zone_id

            return None
        except Exception:
            return None


class PersonTracker:
    """Track persons across frames using bounding box distance."""

    def __init__(self, store_id="STORE_1", camera_id="CAM_1"):
        self.active_tracks = {}
        self.exited_tracks = {}
        self.next_track_id = 0
        self.store_id = store_id
        self.camera_id = camera_id
        self.match_distance = 200
        self.exit_timeout_frames = 90
        self.dwell_interval_frames = 450  # ~30s at 15fps
        self.reentry_window_frames = 450

        # Staff thresholds (sourced from module-level constants)
        self.staff_zone_threshold  = STAFF_MIN_DISTINCT_ZONES
        self.staff_min_frames      = STAFF_MIN_FRAMES
        self.staff_min_span_frames = STAFF_MIN_SPAN_FRAMES

        # ── Debounce state ──
        self._last_exit_frame = {}           # visitor_id  → frame of last exit
        self._last_queue_complete_frame = {} # track_id    → frame of last queue_completed
        self.reentry_cooldown_frames = 450   # 30s at 15fps — suppress flickering reentries
        self.queue_cooldown_frames = 150     # 10s at 15fps — suppress duplicate queue cycles
        self._last_zone_change_frame = {}    # track_id    → frame of last zone transition
        self.zone_change_cooldown = 8        # ~0.5s at 15fps — suppress zone flicker

    def _is_likely_staff(self, track):
        zones_visited = set()
        timestamps = []
        for h in track.get("history", []):
            if h.get("zone"):
                zones_visited.add(h["zone"])
            if h.get("frame") is not None:
                timestamps.append(h["frame"])

        many_zones = len(zones_visited) >= self.staff_zone_threshold
        long_persistence = (
            len(timestamps) >= self.staff_min_frames
            and (max(timestamps) - min(timestamps)) >= self.staff_min_span_frames
        )

        # Both signals required (AND of strong signals)
        return many_zones and long_persistence

    def _evaluate_staff_sticky(self, track):
        """Re-run the heuristic; once a track is staff it stays staff."""
        if track.get("is_staff"):
            return True
        if self._is_likely_staff(track):
            track["is_staff"] = True
            return True
        return False

    def _check_reentry(self, bbox, current_frame):
        x1, y1, x2, y2 = bbox
        cx = (x1 + x2) / 2
        cy = (y1 + y2) / 2

        for track_id, track in self.exited_tracks.items():
            if track.get("last_bbox") is None:
                continue

            # DEBOUNCE: skip if this visitor exited too recently (flickering)
            visitor_id = track["visitor_id"]
            last_exit = self._last_exit_frame.get(visitor_id, 0)
            if current_frame - last_exit < self.reentry_cooldown_frames:
                continue

            lx1, ly1, lx2, ly2 = track["last_bbox"]
            lcx = (lx1 + lx2) / 2
            lcy = (ly1 + ly2) / 2
            dist = np.sqrt((cx - lcx) ** 2 + (cy - lcy) ** 2)
            if dist < self.match_distance * 2:
                return track_id, track
        return None, None

    def update(self, detections, zone_classifier, timestamp, frame_idx):
        events = []
        matched_tracks = set()

        for det in detections:
            try:
                bbox, conf = det
                # Drop very low confidence detections
                if conf < 0.35:
                    continue
                x1, y1, x2, y2 = bbox
                cx = (x1 + x2) / 2
                cy = (y1 + y2) / 2
            except Exception:
                continue

            closest_track_id = None
            min_dist = self.match_distance

            for track_id, track in self.active_tracks.items():
                if track.get("last_bbox") is None:
                    continue
                lx1, ly1, lx2, ly2 = track["last_bbox"]
                dist = np.sqrt((cx - (lx1 + lx2) / 2) ** 2 +
                               (cy - (ly1 + ly2) / 2) ** 2)
                if dist < min_dist:
                    min_dist = dist
                    closest_track_id = track_id

            if closest_track_id is not None:
                track_id = closest_track_id
                matched_tracks.add(track_id)
            else:
                # Pass current_frame for debounce check
                reentry_id, reentry_track = self._check_reentry(bbox, frame_idx)

                if reentry_track is not None:
                    track_id = self.next_track_id
                    self.next_track_id += 1
                    # Carry over staff flag from the previous session — sticky across re-entry
                    inherited_staff = bool(reentry_track.get("is_staff", False))
                    self.active_tracks[track_id] = {
                        "visitor_id": reentry_track["visitor_id"],
                        "history": reentry_track.get("history", []),
                        "last_bbox": None,
                        "last_zone": None,
                        "entry_frame": frame_idx,
                        "last_dwell_frame": {},
                        "frames_unseen": 0,
                        "session_seq": reentry_track.get("session_seq", 0),
                        "is_staff": inherited_staff,
                        "in_billing": False,
                        "billing_join_ts": None,
                    }
                    del self.exited_tracks[reentry_id]
                    matched_tracks.add(track_id)

                    events.append(self._make_entry_event(
                        visitor_id=reentry_track["visitor_id"],
                        timestamp=timestamp,
                        confidence=float(conf),
                        is_staff=inherited_staff,
                        is_reentry=True,
                    ))
                    self.active_tracks[track_id]["session_seq"] += 1
                else:
                    track_id = self.next_track_id
                    self.next_track_id += 1
                    visitor_id = f"ID_{uuid.uuid4().hex[:5].upper()}"
                    self.active_tracks[track_id] = {
                        "visitor_id": visitor_id,
                        "history": [],
                        "last_bbox": None,
                        "last_zone": None,
                        "entry_frame": frame_idx,
                        "last_dwell_frame": {},
                        "frames_unseen": 0,
                        "session_seq": 1,
                        "is_staff": False,
                        "in_billing": False,
                        "billing_join_ts": None,
                    }
                    matched_tracks.add(track_id)

                    # First-ever entry: heuristic can't fire yet (no history). The
                    # API ingestion layer will retroactively flag this row once a
                    # later event in the same batch reports is_staff=True.
                    events.append(self._make_entry_event(
                        visitor_id=visitor_id,
                        timestamp=timestamp,
                        confidence=float(conf),
                        is_staff=False,
                    ))
                    self.active_tracks[track_id]["session_seq"] += 1

            track = self.active_tracks[track_id]
            track["last_bbox"] = bbox
            track["frames_unseen"] = 0

            # ── STICKY STAFF EVALUATION ──
            # Re-run heuristic every frame; once True stays True.
            # Must run BEFORE any event is emitted below so events carry the flag.
            self._evaluate_staff_sticky(track)

            current_zone = zone_classifier.classify(bbox)
            if current_zone and current_zone != "ENTRY":
                if track["last_zone"] != current_zone:
                    # DEBOUNCE: suppress rapid zone flicker
                    last_change = self._last_zone_change_frame.get(track_id, -self.zone_change_cooldown)
                    if frame_idx - last_change < self.zone_change_cooldown:
                        # Too soon — skip this zone transition
                        track["history"].append({"zone": current_zone, "frame": frame_idx})
                        continue

                    self._last_zone_change_frame[track_id] = frame_idx

                    # Zone exit from previous zone
                    if track["last_zone"] and track["last_zone"] != "ENTRY":
                        events.append(self._make_zone_event(
                            event_type="zone_exited",
                            track_id=track_id,
                            zone_id=track["last_zone"],
                            zone_name=zone_classifier.zone_names.get(track["last_zone"], track["last_zone"]),
                            timestamp=timestamp,
                            confidence=float(conf),
                            bbox=bbox,
                            is_staff=track["is_staff"],
                            visitor_id=track["visitor_id"],          # ← IDENTITY FIX
                        ))

                        # If leaving BILLING zone, emit queue_completed
                        if track["last_zone"] == "BILLING" and track.get("in_billing"):
                            billing_count = sum(
                                1 for t in self.active_tracks.values()
                                if t.get("in_billing")
                            )
                            # Track completion frame for debounce
                            self._last_queue_complete_frame[track_id] = frame_idx
                            events.append(self._make_queue_event(
                                event_type="queue_completed",
                                track_id=track_id,
                                zone_id="BILLING",
                                timestamp=timestamp,
                                queue_position=billing_count,
                                confidence=float(conf),
                                bbox=bbox,
                                join_ts=track.get("billing_join_ts", timestamp),
                                is_staff=track["is_staff"],
                                visitor_id=track["visitor_id"],      # ← IDENTITY FIX
                            ))
                            track["in_billing"] = False
                            track["billing_join_ts"] = None

                    # Zone enter new zone
                    events.append(self._make_zone_event(
                        event_type="zone_entered",
                        track_id=track_id,
                        zone_id=current_zone,
                        zone_name=zone_classifier.zone_names.get(current_zone, current_zone),
                        timestamp=timestamp,
                        confidence=float(conf),
                        bbox=bbox,
                        is_staff=track["is_staff"],
                        visitor_id=track["visitor_id"],              # ← IDENTITY FIX
                    ))
                    track["last_dwell_frame"][current_zone] = frame_idx

                    # If entering BILLING, emit queue_joined (with debounce)
                    if current_zone == "BILLING" and not track.get("in_billing"):
                        last_q = self._last_queue_complete_frame.get(track_id, 0)
                        if frame_idx - last_q > self.queue_cooldown_frames:
                            billing_count = sum(
                                1 for t in self.active_tracks.values()
                                if t.get("in_billing")
                            )
                            track["in_billing"] = True
                            track["billing_join_ts"] = timestamp
                            events.append(self._make_queue_event(
                                event_type="queue_joined",
                                track_id=track_id,
                                zone_id=current_zone,
                                timestamp=timestamp,
                                queue_position=billing_count + 1,
                                confidence=float(conf),
                                bbox=bbox,
                                is_staff=track["is_staff"],
                                visitor_id=track["visitor_id"],      # ← IDENTITY FIX
                            ))

                # ZONE_DWELL — emit every 30s of continuous dwell
                if current_zone in track.get("last_dwell_frame", {}):
                    frames_in_zone = frame_idx - track["last_dwell_frame"][current_zone]
                    if frames_in_zone >= self.dwell_interval_frames:
                        dwell_ms = int(frames_in_zone / 15 * 1000)
                        events.append(self._make_zone_event(
                            event_type="zone_dwell",
                            track_id=track_id,
                            zone_id=current_zone,
                            zone_name=zone_classifier.zone_names.get(current_zone, current_zone),
                            timestamp=timestamp,
                            confidence=float(conf),
                            bbox=bbox,
                            dwell_ms=dwell_ms,
                            is_staff=track["is_staff"],
                            visitor_id=track["visitor_id"],          # ← IDENTITY FIX
                        ))
                        track["last_dwell_frame"][current_zone] = frame_idx

                track["last_zone"] = current_zone

            track["history"].append({"zone": current_zone, "frame": frame_idx})

            # Re-evaluate AFTER history append — this is what catches the staff
            # threshold the moment it's crossed, on the same frame.
            self._evaluate_staff_sticky(track)

        for track_id in list(self.active_tracks.keys()):
            if track_id not in matched_tracks:
                self.active_tracks[track_id]["frames_unseen"] += 1

                if self.active_tracks[track_id]["frames_unseen"] > self.exit_timeout_frames:
                    track = self.active_tracks[track_id]
                    # Sticky flag is the source of truth; recompute as a final
                    # safety net for tracks that barely qualify on the last frame.
                    is_staff = self._evaluate_staff_sticky(track)

                    # ── DWELL FIX (Bug #4): emit zone_exited for whatever zone
                    # the visitor was last in, so heatmap dwell pairing works
                    # for tracks that never zone-switched before timing out.
                    if track.get("last_zone") and track["last_zone"] != "ENTRY":
                        events.append(self._make_zone_event(
                            event_type="zone_exited",
                            track_id=track_id,
                            zone_id=track["last_zone"],
                            zone_name=zone_classifier.zone_names.get(
                                track["last_zone"], track["last_zone"]
                            ),
                            timestamp=timestamp,
                            confidence=0.85,
                            bbox=track["last_bbox"],
                            is_staff=is_staff,
                            visitor_id=track["visitor_id"],
                        ))

                    # If still in billing when exiting, emit queue_abandoned
                    if track.get("in_billing"):
                        events.append(self._make_queue_event(
                            event_type="queue_abandoned",
                            track_id=track_id,
                            zone_id="BILLING",
                            timestamp=timestamp,
                            queue_position=0,
                            confidence=0.85,
                            bbox=track["last_bbox"],
                            join_ts=track.get("billing_join_ts", timestamp),
                            is_staff=is_staff,
                            visitor_id=track["visitor_id"],          # ← IDENTITY FIX
                        ))

                    # Track exit frame for reentry debounce
                    self._last_exit_frame[track["visitor_id"]] = frame_idx

                    events.append(self._make_exit_event(
                        visitor_id=track["visitor_id"],
                        timestamp=timestamp,
                        confidence=0.85,
                        is_staff=is_staff,
                    ))

                    self.exited_tracks[track_id] = {
                        "visitor_id": track["visitor_id"],
                        "last_bbox": track["last_bbox"],
                        "exit_frame": frame_idx,
                        "history": track["history"],
                        "session_seq": track["session_seq"] + 1,
                        "is_staff": is_staff,   # carry to potential reentry
                    }
                    del self.active_tracks[track_id]

        # Cleanup old exited tracks
        for track_id in list(self.exited_tracks.keys()):
            if frame_idx - self.exited_tracks[track_id].get("exit_frame", 0) > self.reentry_window_frames:
                del self.exited_tracks[track_id]

        return events

    def _make_entry_event(self, visitor_id, timestamp, confidence=0.9, is_staff=False, is_reentry=False):
        return {
            "event_type": "reentry" if is_reentry else "entry",
            "id_token": visitor_id,
            "store_code": self.store_id,
            "camera_id": self.camera_id,
            "event_timestamp": timestamp,
            "is_staff": is_staff,
            "gender_pred": None,
            "age_pred": None,
            "age_bucket": None,
            "is_face_hidden": confidence < 0.6,
            "group_id": None,
            "group_size": None,
        }

    def _make_exit_event(self, visitor_id, timestamp, confidence=0.85, is_staff=False):
        return {
            "event_type": "exit",
            "id_token": visitor_id,
            "store_code": self.store_id,
            "camera_id": self.camera_id,
            "event_timestamp": timestamp,
            "is_staff": is_staff,
            "gender_pred": None,
            "age_pred": None,
            "age_bucket": None,
            "is_face_hidden": False,
            "group_id": None,
            "group_size": None,
        }

    def _make_zone_event(self, event_type, track_id, zone_id, zone_name, timestamp,
                         confidence=0.9, bbox=None, dwell_ms=None, is_staff=False,
                         visitor_id=None):
        cx = (bbox[0] + bbox[2]) / 2 if bbox else 0
        cy = (bbox[1] + bbox[3]) / 2 if bbox else 0
        evt = {
            "event_type": event_type,
            "track_id": track_id,
            "id_token": visitor_id,            # ← IDENTITY FIX: same person across event types
            "store_id": self.store_id,
            "camera_id": self.camera_id,
            "zone_id": zone_id,
            "zone_name": zone_name,
            "zone_type": "BILLING" if zone_id == "BILLING" else "SHELF",
            "is_revenue_zone": "Yes",
            "event_time": timestamp,
            "zone_hotspot_x": round(cx, 1),
            "zone_hotspot_y": round(cy, 1),
            "gender": None,
            "age": None,
            "age_bucket": None,
            "is_staff": is_staff,
        }
        if dwell_ms is not None:
            evt["dwell_ms"] = dwell_ms
        return evt

    def _make_queue_event(self, event_type, track_id, zone_id, timestamp,
                          queue_position=1, confidence=0.9, bbox=None,
                          join_ts=None, is_staff=False, visitor_id=None):
        cx = (bbox[0] + bbox[2]) / 2 if bbox else 0
        cy = (bbox[1] + bbox[3]) / 2 if bbox else 0

        wait_seconds = 0
        if join_ts and event_type in ("queue_completed", "queue_abandoned"):
            try:
                join_dt = datetime.fromisoformat(join_ts)
                exit_dt = datetime.fromisoformat(timestamp)
                wait_seconds = max(0, int((exit_dt - join_dt).total_seconds()))
            except Exception:
                pass

        return {
            "queue_event_id": str(uuid.uuid4()),
            "event_type": event_type,
            "track_id": track_id,
            "id_token": visitor_id,            # ← IDENTITY FIX
            "store_id": self.store_id,
            "camera_id": self.camera_id,
            "zone_id": zone_id,
            "zone_name": "Billing Counter Queue",
            "zone_type": "BILLING",
            "is_revenue_zone": "Yes",
            "queue_join_ts": join_ts or timestamp,
            "queue_served_ts": timestamp if event_type == "queue_completed" else None,
            "queue_exit_ts": timestamp if event_type != "queue_joined" else None,
            "wait_seconds": wait_seconds,
            "queue_position_at_join": queue_position,
            "abandoned": event_type == "queue_abandoned",
            "zone_hotspot_x": round(cx, 1),
            "zone_hotspot_y": round(cy, 1),
            "gender": None,
            "age": None,
            "age_bucket": None,
            "is_staff": is_staff,
        }

    # Keep legacy _make_event for backward compat with tests
    def _make_event(self, visitor_id, event_type, timestamp, zone_id=None,
                    dwell_ms=0, confidence=0.9, is_staff=False,
                    queue_depth=None, sku_zone=None, session_seq=1):
        if event_type in ("ENTRY", "EXIT"):
            return self._make_entry_event(visitor_id, timestamp, confidence, is_staff) \
                if event_type == "ENTRY" else self._make_exit_event(visitor_id, timestamp, confidence, is_staff)
        elif event_type in ("ZONE_ENTER", "ZONE_EXIT"):
            return self._make_zone_event(
                "zone_entered" if event_type == "ZONE_ENTER" else "zone_exited",
                0, zone_id or "", zone_id or "", timestamp, confidence,
                visitor_id=visitor_id,
            )
        elif event_type == "BILLING_QUEUE_JOIN":
            return self._make_queue_event("queue_joined", 0, zone_id or "BILLING", timestamp,
                                          queue_depth or 1, confidence, visitor_id=visitor_id)
        else:
            return {
                "event_type": event_type.lower(),
                "id_token": visitor_id,
                "store_code": self.store_id,
                "camera_id": self.camera_id,
                "event_timestamp": timestamp,
                "is_staff": is_staff,
            }


def process_clip(video_path, camera_id, store_id, layout_path, clip_start_time=None):
    """Process a CCTV clip and emit structured events."""
    print(f"\n🎬 Processing: {video_path}")
    print(f"   Camera: {camera_id} | Store: {store_id}")

    model = YOLO("yolov8n.pt")
    print("   ✅ YOLOv8n loaded")

    zone_classifier = ZoneClassifier(layout_path, store_id=store_id)
    tracker = PersonTracker(store_id=store_id, camera_id=camera_id)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"   ❌ Could not open video: {video_path}")
        return []

    fps = cap.get(cv2.CAP_PROP_FPS) or 15
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    zone_classifier.set_frame_size(frame_w, frame_h)

    if clip_start_time is None:
        clip_start_time = datetime.now(timezone.utc)

    print(f"   Resolution: {frame_w}x{frame_h} | FPS: {fps:.1f} | Frames: {total_frames}")

    frame_idx = 0
    all_events = []
    process_every_n = 5

    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break

        frame_idx += 1
        if frame_idx % process_every_n != 0:
            continue

        timestamp = (clip_start_time + timedelta(seconds=frame_idx / fps)).isoformat()
        try:
            results = model(frame, classes=[0], conf=0.35, imgsz=640, verbose=False)
            detections = []
            for r in results:
                for box in r.boxes:
                    bbox = box.xyxy[0].cpu().numpy().tolist()
                    conf = float(box.conf[0])
                    detections.append((bbox, conf))

            events = tracker.update(detections, zone_classifier, timestamp, frame_idx)
            all_events.extend(events)
        except Exception as e:
            print(f"   ⚠️ Frame {frame_idx} error: {e}")
            continue

        if frame_idx % 300 == 0:
            pct = round(frame_idx / total_frames * 100, 1) if total_frames else 0
            print(f"   📊 Frame {frame_idx}/{total_frames} ({pct}%) | Events: {len(all_events)}")

    cap.release()

    # Close remaining active tracks as exits
        # Close remaining active tracks as exits
    final_ts = (clip_start_time + timedelta(seconds=frame_idx / fps)).isoformat()
    for track_id, track in list(tracker.active_tracks.items()):
        # Use sticky flag (re-evaluate as final safety net)
        is_staff = tracker._evaluate_staff_sticky(track)

        # ── DWELL FIX (Bug #4): close any still-open zone at end of clip
        # so heatmap dwell pairing works for visitors who never zone-switched.
        if track.get("last_zone") and track["last_zone"] != "ENTRY":
            all_events.append(tracker._make_zone_event(
                event_type="zone_exited",
                track_id=track_id,
                zone_id=track["last_zone"],
                zone_name=zone_classifier.zone_names.get(
                    track["last_zone"], track["last_zone"]
                ),
                timestamp=final_ts,
                confidence=0.80,
                bbox=track["last_bbox"],
                is_staff=is_staff,
                visitor_id=track["visitor_id"],
            ))

        if track.get("in_billing"):
            all_events.append(tracker._make_queue_event(
                event_type="queue_abandoned",
                track_id=track_id,
                zone_id="BILLING",
                timestamp=final_ts,
                queue_position=0,
                confidence=0.80,
                bbox=track["last_bbox"],
                join_ts=track.get("billing_join_ts", final_ts),
                is_staff=is_staff,
                visitor_id=track["visitor_id"],          # ← IDENTITY FIX
            ))

        all_events.append(tracker._make_exit_event(
            visitor_id=track["visitor_id"],
            timestamp=final_ts,
            confidence=0.80,
            is_staff=is_staff,
        ))
    # Per-clip summary
    print(f"\n   ✅ Done: {len(all_events)} events from {frame_idx} frames")
    event_types = {}
    staff_event_count = 0
    distinct_visitors = set()
    for e in all_events:
        t = e["event_type"]
        event_types[t] = event_types.get(t, 0) + 1
        if e.get("is_staff"):
            staff_event_count += 1
        # Identity-fix sanity check: every event should now have an id_token
        vid = e.get("id_token") or e.get("visitor_id")
        if vid:
            distinct_visitors.add(vid)
    for t, count in sorted(event_types.items()):
        print(f"      {t}: {count}")
    print(f"      (staff-flagged events: {staff_event_count})")
    print(f"      (distinct visitor_ids across all event types: {len(distinct_visitors)})")

    return all_events


def process_all_stores(layout_path="data/store_layout.json", start_time=None):
    """Process all clips from all stores defined in store_layout.json."""
    with open(layout_path) as f:
        layout = json.load(f)

    if start_time is None:
        start_time = datetime.now(timezone.utc)

    all_events = []

    for store_id, store_config in layout.items():
        cameras = store_config.get("cameras", {})
        print(f"\n{'='*60}")
        print(f"🏪 Store: {store_id} | Cameras: {len(cameras)}")
        print(f"{'='*60}")

        for camera_id, video_path in cameras.items():
            if not Path(video_path).exists():
                print(f"   ⚠️ Video not found: {video_path} — skipping")
                continue

            events = process_clip(
                video_path=video_path,
                camera_id=camera_id,
                store_id=store_id,
                layout_path=layout_path,
                clip_start_time=start_time
            )

            output_path = f"data/processed/{store_id}_{camera_id}_events.jsonl"
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w") as f:
                for event in events:
                    f.write(json.dumps(event) + "\n")
            print(f"   📁 Saved → {output_path}")

            all_events.extend(events)

    combined_path = "data/processed/all_events.jsonl"
    with open(combined_path, "w") as f:
        for event in all_events:
            f.write(json.dumps(event) + "\n")

    print(f"\n{'='*60}")
    print(f"📊 TOTAL: {len(all_events)} events across all stores")
    print(f"📁 Combined → {combined_path}")

    return all_events


def main():
    parser = argparse.ArgumentParser(description="CCTV Detection Pipeline")
    parser.add_argument("--clip", default=None, help="Path to single video clip")
    parser.add_argument("--camera_id", default="CAM_1", help="Camera ID")
    parser.add_argument("--store_id", default="STORE_1", help="Store ID")
    parser.add_argument("--layout", default="data/store_layout.json", help="Store layout JSON")
    parser.add_argument("--output", default=None, help="Output JSONL file path")
    parser.add_argument("--start_time", default=None, help="Clip start time ISO format")
    parser.add_argument("--all", action="store_true", help="Process all stores and cameras")
    args = parser.parse_args()

    clip_start = None
    if args.start_time:
        try:
            clip_start = datetime.fromisoformat(args.start_time.replace("Z", "+00:00"))
        except Exception:
            clip_start = None

    if args.all or args.clip is None:
        process_all_stores(layout_path=args.layout, start_time=clip_start)
    else:
        events = process_clip(
            video_path=args.clip,
            camera_id=args.camera_id,
            store_id=args.store_id,
            layout_path=args.layout,
            clip_start_time=clip_start
        )
        if args.output is None:
            args.output = f"data/processed/{args.camera_id}_events.jsonl"
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w") as f:
            for event in events:
                f.write(json.dumps(event) + "\n")
        print(f"\n📁 Saved {len(events)} events → {args.output}")


if __name__ == "__main__":
    main()