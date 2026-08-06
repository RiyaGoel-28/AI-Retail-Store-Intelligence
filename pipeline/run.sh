#!/bin/bash
# ============================================================
# Store Intelligence — Detection Pipeline Runner (resilient)
# Usage: bash pipeline/run.sh
#
# Discovers all .mp4 / .avi / .mov files under data/CCTV*/<store>/
# and processes them. Falls back to store_layout.json camera
# mappings when available. Works regardless of filename scheme.
# ============================================================

set -e

OUTPUT_DIR="data/processed"
LAYOUT="data/store_layout.json"
API_URL="${API_URL:-http://localhost:8000}"
START_TIME="${START_TIME:-2026-03-08T18:00:00Z}"
CCTV_ROOT="${CCTV_ROOT:-data/CCTV Footage}"

echo "=============================================="
echo "🎥 Store Intelligence — Detection Pipeline"
echo "=============================================="
echo "Layout:    $LAYOUT"
echo "API:       $API_URL"
echo "CCTV root: $CCTV_ROOT"
echo ""

# ── Prerequisites ─────────────────────────────────
if ! command -v python &> /dev/null; then
    echo "❌ Python not found"
    exit 1
fi

if [ ! -f "$LAYOUT" ]; then
    echo "⚠️  Store layout not found: $LAYOUT"
    echo "    Pipeline will use built-in default zones."
fi

mkdir -p "$OUTPUT_DIR"

# ── Load POS data (if present) ────────────────────
echo "📦 Loading POS transactions..."
POS_FILE=$(find data/ -maxdepth 2 -type f \( -iname "POS*.csv" -o -iname "pos*.csv" \) 2>/dev/null | head -1)
if [ -n "$POS_FILE" ]; then
    echo "   Found: $POS_FILE"
    python pipeline/load_pos.py "$POS_FILE" || echo "   ⚠️ POS load failed — continuing"
else
    echo "   ⚠️ No POS CSV found in data/ — skipping"
fi
echo ""

# ── Verify API is reachable ───────────────────────
echo "🔍 Checking API..."
if curl -s "$API_URL/health" > /dev/null 2>&1; then
    echo "   ✅ API is running"
else
    echo "   ❌ API not reachable at $API_URL"
    echo "      Start it with: docker compose up -d"
    exit 1
fi
echo ""

# ── Auto-discover stores and clips ────────────────
# Strategy:
#   1. Look for store_layout.json — use its camera mappings if present
#   2. Otherwise, scan $CCTV_ROOT for sub-directories — treat each as a store
#   3. Within each store dir, pick up every video file
# ──────────────────────────────────────────────────

discover_and_process_store() {
    local STORE_ID="$1"
    local STORE_DIR="$2"

    if [ ! -d "$STORE_DIR" ]; then
        echo "   ⚠️  Directory not found: $STORE_DIR — skipping"
        return
    fi

    echo "=============================================="
    echo "🏪 Store: $STORE_ID  ($STORE_DIR)"
    echo "=============================================="

    # find all video files under this store directory
    local VIDEOS
    VIDEOS=$(find "$STORE_DIR" -maxdepth 2 -type f \( -iname "*.mp4" -o -iname "*.avi" -o -iname "*.mov" -o -iname "*.mkv" \) 2>/dev/null)

    if [ -z "$VIDEOS" ]; then
        echo "   ⚠️  No video clips found under $STORE_DIR"
        return
    fi

    while IFS= read -r CLIP; do
        # Derive a camera_id from the filename (sanitised)
        local FILENAME
        FILENAME=$(basename "$CLIP")
        local CAM_ID
        CAM_ID=$(echo "$FILENAME" | sed -E 's/\.(mp4|avi|mov|mkv)$//I' | tr ' ' '_' | tr -cd '[:alnum:]_-')
        [ -z "$CAM_ID" ] && CAM_ID="CAM_DEFAULT"

        echo "🎬 $CAM_ID  ←  $CLIP"
        python pipeline/detect.py \
            --clip "$CLIP" \
            --camera_id "$CAM_ID" \
            --store_id "$STORE_ID" \
            --layout "$LAYOUT" \
            --output "$OUTPUT_DIR/${STORE_ID}_${CAM_ID}_events.jsonl" \
            --start_time "$START_TIME" \
            || echo "   ⚠️ detect.py failed for $CLIP — continuing"
        echo ""
    done <<< "$VIDEOS"
}

# Method 1: read store list from layout
STORES=()
if [ -f "$LAYOUT" ]; then
    LAYOUT_STORES=$(python -c "
import json, sys
try:
    with open('$LAYOUT') as f:
        layout = json.load(f)
    if isinstance(layout, dict):
        for k in layout.keys():
            print(k)
except Exception:
    pass
" 2>/dev/null)
    if [ -n "$LAYOUT_STORES" ]; then
        while IFS= read -r s; do STORES+=("$s"); done <<< "$LAYOUT_STORES"
    fi
fi

# Method 2: fall back to scanning $CCTV_ROOT
if [ ${#STORES[@]} -eq 0 ] && [ -d "$CCTV_ROOT" ]; then
    while IFS= read -r d; do
        SHORT=$(basename "$d")
        # Default store id from folder name (strip spaces)
        SID=$(echo "$SHORT" | tr ' ' '_')
        STORES+=("$SID")
    done < <(find "$CCTV_ROOT" -mindepth 1 -maxdepth 1 -type d 2>/dev/null)
fi

if [ ${#STORES[@]} -eq 0 ]; then
    echo "⚠️  No stores discovered. Place clips under $CCTV_ROOT/<store>/ or define them in $LAYOUT."
    exit 0
fi

# Process each store
for STORE_ID in "${STORES[@]}"; do
    # Resolve the directory:
    #   Prefer "$CCTV_ROOT/<store_id>"
    #   else "$CCTV_ROOT/Store <N>"  (legacy)
    #   else first directory whose name contains the store id
    STORE_DIR=""
    for CANDIDATE in \
        "$CCTV_ROOT/$STORE_ID" \
        "$CCTV_ROOT/${STORE_ID//_/ }" \
        "$CCTV_ROOT/$(echo "$STORE_ID" | tr '_' ' ')"
    do
        if [ -d "$CANDIDATE" ]; then
            STORE_DIR="$CANDIDATE"
            break
        fi
    done

    # Last resort: any subdir whose name contains the store id
    if [ -z "$STORE_DIR" ] && [ -d "$CCTV_ROOT" ]; then
        STORE_DIR=$(find "$CCTV_ROOT" -mindepth 1 -maxdepth 1 -type d -iname "*${STORE_ID}*" | head -1)
    fi

    # Final fallback: just use $CCTV_ROOT itself
    [ -z "$STORE_DIR" ] && STORE_DIR="$CCTV_ROOT"

    discover_and_process_store "$STORE_ID" "$STORE_DIR"
done

# ── Emit all events to API ────────────────────────
echo "=============================================="
echo "📤 Ingesting all events into API..."
echo "=============================================="

shopt -s nullglob 2>/dev/null || true
for EVENTS_FILE in "$OUTPUT_DIR"/*_events.jsonl; do
    if [ -f "$EVENTS_FILE" ]; then
        echo ""
        echo "📂 Emitting $(basename "$EVENTS_FILE")..."
        python pipeline/emit.py --input "$EVENTS_FILE" --api_url "$API_URL" \
            || echo "   ⚠️ emit.py failed — continuing"
    fi
done

# Optional: ingest sample_events.jsonl if reviewer dropped it in data/
if [ -f "data/sample_events.jsonl" ]; then
    echo ""
    echo "📂 Emitting sample_events.jsonl..."
    python pipeline/emit.py --input "data/sample_events.jsonl" --api_url "$API_URL" || true
fi

# ── Summary ───────────────────────────────────────
echo ""
echo "=============================================="
echo "✅ Pipeline Complete"
echo "=============================================="
echo ""
for STORE_ID in "${STORES[@]}"; do
    echo "🔍 $STORE_ID metrics:"
    curl -s "$API_URL/stores/$STORE_ID/metrics" | python -m json.tool 2>/dev/null || echo "   No data"
    echo ""
done
echo "🔍 Health:"
curl -s "$API_URL/health" | python -m json.tool 2>/dev/null
echo ""
echo "📊 Dashboard: $API_URL/"
echo "=============================================="