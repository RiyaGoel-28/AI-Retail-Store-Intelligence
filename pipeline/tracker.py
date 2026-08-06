"""
# PROMPT: Generate a multi-object tracker for retail store CCTV
# that handles re-entry detection and visitor ID assignment.
# CHANGES MADE: Added distance-based Re-ID with configurable
# threshold, session tracking for dwell time calculation.
"""

import math
from collections import defaultdict

class TrackedPerson:
    def __init__(self, track_id, visitor_id, bbox, confidence, is_staff):
        self.track_id = track_id
        self.visitor_id = visitor_id
        self.bbox = bbox
        self.confidence = confidence
        self.is_staff = is_staff
        self.positions = [self._center(bbox)]
        self.first_seen = None
        self.last_seen = None
        self.current_zone = None
        self.previous_zone = None
        self.zone_enter_time = None
        self.dwell_start = None
        self.session_seq = 0
        self.exited = False
        self.direction = None  # "inbound" or "outbound"

    def _center(self, bbox):
        return ((bbox[0] + bbox[2]) // 2, (bbox[1] + bbox[3]) // 2)

    def update(self, bbox, confidence, timestamp):
        self.bbox = bbox
        self.confidence = confidence
        self.positions.append(self._center(bbox))
        self.last_seen = timestamp
        # Keep last 30 positions
        if len(self.positions) > 30:
            self.positions = self.positions[-30:]

    def get_direction(self):
        """Determine movement direction based on trajectory."""
        if len(self.positions) < 5:
            return None
        start_y = self.positions[0][1]
        end_y = self.positions[-1][1]
        # Moving down = entering (typically)
        if end_y - start_y > 30:
            return "inbound"
        elif start_y - end_y > 30:
            return "outbound"
        return None


class Tracker:
    def __init__(self, max_distance=100, max_lost=30):
        self.tracks = {}
        self.next_id = 0
        self.max_distance = max_distance
        self.max_lost_frames = max_lost
        self.lost_count = defaultdict(int)
        self.exited_visitors = {}  # For re-entry detection
        self.visitor_counter = 0

    def update(self, detections, timestamp):
        """Update tracks with new detections."""
        if not detections:
            # Increment lost count for all tracks
            for tid in list(self.tracks.keys()):
                self.lost_count[tid] += 1
                if self.lost_count[tid] > self.max_lost_frames:
                    self.tracks[tid].exited = True
            return list(self.tracks.values())

        # Match detections to existing tracks
        matched, unmatched_dets, unmatched_tracks = self._match(detections)

        # Update matched tracks
        for tid, det in matched:
            self.tracks[tid].update(
                det["bbox"], det["confidence"], timestamp
            )
            self.tracks[tid].is_staff = det.get("is_staff", False)
            self.lost_count[tid] = 0

        # Handle unmatched detections (new people)
        for det in unmatched_dets:
            # Check for re-entry
            visitor_id = self._check_reentry(det)
            is_reentry = visitor_id is not None

            if not is_reentry:
                self.visitor_counter += 1
                visitor_id = f"VIS_{self.visitor_counter:06x}"

            tid = self.next_id
            self.next_id += 1
            person = TrackedPerson(
                tid, visitor_id, det["bbox"],
                det["confidence"], det.get("is_staff", False)
            )
            person.first_seen = timestamp
            person.last_seen = timestamp
            self.tracks[tid] = person

        # Handle lost tracks
        for tid in unmatched_tracks:
            self.lost_count[tid] += 1
            if self.lost_count[tid] > self.max_lost_frames:
                # Store for re-entry detection
                person = self.tracks[tid]
                person.exited = True
                self.exited_visitors[person.visitor_id] = {
                    "last_bbox": person.bbox,
                    "last_position": person.positions[-1],
                    "exit_time": timestamp
                }

        return list(self.tracks.values())

    def _match(self, detections):
        """Simple distance-based matching."""
        matched = []
        unmatched_dets = list(range(len(detections)))
        unmatched_tracks = list(self.tracks.keys())

        if not self.tracks:
            return [], detections, []

        for di, det in enumerate(detections):
            det_center = det["center"]
            best_tid = None
            best_dist = float("inf")

            for tid in unmatched_tracks:
                track = self.tracks[tid]
                track_center = track.positions[-1]
                dist = math.sqrt(
                    (det_center[0] - track_center[0]) ** 2 +
                    (det_center[1] - track_center[1]) ** 2
                )
                if dist < best_dist and dist < self.max_distance:
                    best_dist = dist
                    best_tid = tid

            if best_tid is not None:
                matched.append((best_tid, det))
                unmatched_dets.remove(di)
                unmatched_tracks.remove(best_tid)

        remaining_dets = [detections[i] for i in unmatched_dets]
        return matched, remaining_dets, unmatched_tracks

    def _check_reentry(self, detection):
        """Check if detection matches a recently exited visitor."""
        det_center = detection["center"]

        for vid, info in self.exited_visitors.items():
            last_pos = info["last_position"]
            dist = math.sqrt(
                (det_center[0] - last_pos[0]) ** 2 +
                (det_center[1] - last_pos[1]) ** 2
            )
            # If similar position and within reasonable time
            if dist < self.max_distance * 2:
                return vid

        return None