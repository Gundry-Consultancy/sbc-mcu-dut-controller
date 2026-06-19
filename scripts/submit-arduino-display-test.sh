#!/usr/bin/env bash
# Submit a WipperSnapper Arduino display test job to the HIL controller.
# Builds firmware with PlatformIO on the bench host, flashes the device,
# then drives the display via a protoMQ play script and runs pytest.
#
# Usage:
#   HIL_API_TOKEN=your-token bash scripts/submit-arduino-display-test.sh
#
# Required env:
#   HIL_API_TOKEN          HIL controller bearer token (or use HIL_OIDC_TOKEN)
#   WIFI_SSID              Bench WiFi SSID for the device to join
#   WIFI_PASSWORD          Bench WiFi password
#
# Optional overrides:
#   HIL_API_BASE           default: http://localhost:8080
#   DEVICE_ID              HIL device id, default: display-2900
#   SERIAL_PORT            Serial port on the worker host, default: /dev/ttyACM0
#   WIPPERSNAPPER_REPO     default: https://github.com/adafruit/Adafruit_WipperSnapper_Arduino.git
#   WIPPERSNAPPER_REF      WipperSnapper Arduino git ref, default: main
#   PROTOMQ_REPO           default: https://github.com/tyeth/protomq.git
#   PROTOMQ_REF            protoMQ git ref, default: main
#   MQTT_HOST              ProtoMQ broker host, default: rpi-displays.local
#   MQTT_PORT              ProtoMQ MQTT port, default: 1884
#   IO_USERNAME            Adafruit IO username (leave blank for local ProtoMQ only)
#   IO_KEY                 Adafruit IO key

set -euo pipefail

: "${HIL_API_TOKEN:?Set HIL_API_TOKEN to a HIL controller bearer token}"
: "${WIFI_SSID:?Set WIFI_SSID to the bench WiFi network name}"
: "${WIFI_PASSWORD:?Set WIFI_PASSWORD to the bench WiFi password}"

HIL_API_BASE="${HIL_API_BASE:-http://localhost:8080}"
DEVICE_ID="${DEVICE_ID:-display-2900}"
SERIAL_PORT="${SERIAL_PORT:-/dev/ttyACM0}"
WIPPERSNAPPER_REPO="${WIPPERSNAPPER_REPO:-https://github.com/adafruit/Adafruit_WipperSnapper_Arduino.git}"
WIPPERSNAPPER_REF="${WIPPERSNAPPER_REF:-main}"
PROTOMQ_REPO="${PROTOMQ_REPO:-https://github.com/tyeth/protomq.git}"
PROTOMQ_REF="${PROTOMQ_REF:-main}"
MQTT_HOST="${MQTT_HOST:-rpi-displays.local}"
MQTT_PORT="${MQTT_PORT:-1884}"
IO_USERNAME="${IO_USERNAME:-}"
IO_KEY="${IO_KEY:-}"
GH_PAT="${GH_PAT:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JOB_TEMPLATE="${SCRIPT_DIR}/../examples/arduino-display/job.json"

tmp_job=$(mktemp /tmp/hil-arduino-display-XXXXXX.json)
trap 'rm -f "$tmp_job"' EXIT

# Build the setup command: git clone protomq, pip install pio, build, flash, install deps
SETUP_CMD="git clone --depth 1 --branch ${PROTOMQ_REF} ${PROTOMQ_REPO} protomq && pip install platformio && pio run -e adafruit_feather_esp32s3 && pio run -e adafruit_feather_esp32s3 --target upload --upload-port ${SERIAL_PORT} && pip install -e . && pip install -e protomq/ && envsubst < examples/wippersnapper-arduino/secrets.example.json > .pio/build/adafruit_feather_esp32s3/secrets.json"

jq \
  --arg device_id    "$DEVICE_ID" \
  --arg ws_repo      "$WIPPERSNAPPER_REPO" \
  --arg ws_ref       "$WIPPERSNAPPER_REF" \
  --arg proto_ref    "$PROTOMQ_REF" \
  --arg serial_port  "$SERIAL_PORT" \
  --arg mqtt_host    "$MQTT_HOST" \
  --arg mqtt_port    "$MQTT_PORT" \
  --arg io_user      "$IO_USERNAME" \
  --arg io_key       "$IO_KEY" \
  --arg gh_pat       "$GH_PAT" \
  --arg wifi_ssid    "$WIFI_SSID" \
  --arg wifi_pass    "$WIFI_PASSWORD" \
  --arg setup_cmd    "$SETUP_CMD" \
  '
    .target.device.id                      = $device_id     |
    .payload.source.repo                   = $ws_repo       |
    .payload.source.ref                    = $ws_ref        |
    .payload.source.pat                    = $gh_pat        |
    .payload.source.setup                  = ["bash", "-c", $setup_cmd] |
    .params.protomq.broker_host            = $mqtt_host     |
    .secrets.MQTT_HOST                     = $mqtt_host     |
    .secrets.MQTT_PORT                     = $mqtt_port     |
    .secrets.HIL_PROTOMQ_HOST             = $mqtt_host     |
    .secrets.HIL_PROTOMQ_PORT             = $mqtt_port     |
    .secrets.HIL_WIFI_SSID                = $wifi_ssid     |
    .secrets.HIL_WIFI_PASSWORD            = $wifi_pass     |
    (if $io_user != "" then .secrets.IO_USERNAME = $io_user | .secrets.HIL_IO_USERNAME = $io_user else . end) |
    (if $io_key  != "" then .secrets.IO_KEY      = $io_key  | .secrets.HIL_IO_KEY      = $io_key  else . end) |
    (if $gh_pat  == "" then del(.payload.source.pat) else . end) |
    .metadata.wippersnapper_ref            = $ws_ref        |
    .metadata.protomq_ref                 = $proto_ref
  ' \
  "$JOB_TEMPLATE" > "$tmp_job"

echo "Submitting Arduino display test job:"
echo "  device       : ${DEVICE_ID}"
echo "  WS Arduino   : ${WIPPERSNAPPER_REPO} @ ${WIPPERSNAPPER_REF}"
echo "  protoMQ      : ${PROTOMQ_REPO} @ ${PROTOMQ_REF}"
echo "  MQTT broker  : ${MQTT_HOST}:${MQTT_PORT}"
echo "  serial port  : ${SERIAL_PORT}"
echo ""

HIL_API_BASE="$HIL_API_BASE" \
HIL_API_TOKEN="$HIL_API_TOKEN" \
HIL_JOB_JSON="$tmp_job" \
  bash "${SCRIPT_DIR}/../examples/hil-call.sh"
