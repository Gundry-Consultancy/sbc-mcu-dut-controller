#!/usr/bin/env bash
# Submit a Wippersnapper_Python non-hardware test run to the HIL controller.
#
# Usage:
#   GH_PAT=ghp_xxx HIL_API_TOKEN=your-token bash scripts/submit-wipper-test.sh
#
# Optional overrides:
#   HIL_API_BASE     default: http://localhost:8080
#   WIPPER_REF       git ref to test, default: main
#   PYTEST_MARKERS   pytest -m expression, default: "not hardware"
#   IO_USERNAME      Adafruit IO username (written to .env if set)
#   IO_KEY           Adafruit IO key (written to .env if set)
#   MQTT_HOST        ProtoMQ broker host, default: pi5-protomq.local
#   MQTT_PORT        ProtoMQ broker MQTT port, default: 1884

set -euo pipefail

: "${GH_PAT:?Set GH_PAT to a GitHub personal access token with repo read scope}"
: "${HIL_API_TOKEN:?Set HIL_API_TOKEN to a HIL controller bearer token}"

HIL_API_BASE="${HIL_API_BASE:-http://localhost:8080}"
WIPPER_REF="${WIPPER_REF:-main}"
PYTEST_MARKERS="${PYTEST_MARKERS:-not hardware}"
IO_USERNAME="${IO_USERNAME:-}"
IO_KEY="${IO_KEY:-}"
MQTT_HOST="${MQTT_HOST:-pi5-protomq.local}"
MQTT_PORT="${MQTT_PORT:-1884}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JOB_TEMPLATE="${SCRIPT_DIR}/../examples/wippersnapper-python/job.json"

tmp_job=$(mktemp /tmp/hil-wipper-job-XXXXXX.json)
trap 'rm -f "$tmp_job"' EXIT

jq \
  --arg pat      "$GH_PAT" \
  --arg ref      "$WIPPER_REF" \
  --arg mqtt_host "$MQTT_HOST" \
  --arg mqtt_port "$MQTT_PORT" \
  --arg io_user  "$IO_USERNAME" \
  --arg io_key   "$IO_KEY" \
  --argjson markers "$(jq -n --arg m "$PYTEST_MARKERS" '["-m", $m, "-v", "--tb=short"]')" \
  '
    .payload.source.pat        = $pat     |
    .payload.source.ref        = $ref     |
    .params.args               = $markers |
    .params.protomq.broker_host = $mqtt_host |
    .secrets.MQTT_HOST         = $mqtt_host  |
    .secrets.MQTT_PORT         = $mqtt_port  |
    (if $io_user != "" then .secrets.IO_USERNAME = $io_user else . end) |
    (if $io_key  != "" then .secrets.IO_KEY      = $io_key  else . end)
  ' \
  "$JOB_TEMPLATE" > "$tmp_job"

echo "Submitting job (ref=${WIPPER_REF}, markers=\"${PYTEST_MARKERS}\", broker=${MQTT_HOST})..."

HIL_API_BASE="$HIL_API_BASE" \
HIL_API_TOKEN="$HIL_API_TOKEN" \
HIL_JOB_JSON="$tmp_job" \
  bash "${SCRIPT_DIR}/../examples/hil-call.sh"
