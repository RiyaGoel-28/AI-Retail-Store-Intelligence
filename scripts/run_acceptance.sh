#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

echo "Starting services with docker compose..."
docker compose up -d --build

API_URL="http://localhost:8000"
HEALTH_URL="$API_URL/health"

# wait for health
echo "Waiting for API to become healthy at $HEALTH_URL (timeout 120s)"
SECS=0
until curl -sSf "$HEALTH_URL" > /dev/null || [ $SECS -ge 120 ]; do
  sleep 2
  SECS=$((SECS+2))
  printf "."
done

if [ $SECS -ge 120 ]; then
  echo "Timed out waiting for API health"
  docker compose logs --no-color
  exit 2
fi

echo "API is healthy. Posting sample events..."
SAMPLE_FILE="scripts/sample_events_acceptance.json"

resp=$(curl -s -X POST "$API_URL/events/ingest" -H "Content-Type: application/json" --data-binary "@${SAMPLE_FILE}")
if [ $? -ne 0 ]; then
  echo "Failed to POST events"
  docker compose logs --no-color
  exit 3
fi

echo "Ingest response:"
echo "$resp" | jq .

# run validator for the sample file
python3 scripts/validate_events.py --file "$SAMPLE_FILE"

# Query endpoints
echo "Querying metrics, funnel, heatmap, anomalies"
STORE_ID=$(jq -r '.[0].store_id' "$SAMPLE_FILE")

for endpoint in metrics funnel heatmap anomalies; do
  url="$API_URL/stores/${STORE_ID}/${endpoint}"
  echo "GET $url"
  out=$(curl -s "$url")
  echo "$out" | jq .
done

echo "Acceptance run completed successfully"
