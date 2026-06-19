# Test Plan: Adafruit FeatherWing OLED 128×32 (#2900) — WipperSnapper Arduino

**Status:** Draft — v1 CLI/API invocation  
**Product:** [Adafruit FeatherWing OLED 128×32 (SSD1306)](https://www.adafruit.com/product/2900)  
**HIL device:** `display-2900` on host `rpi-displays`  
**Date:** 2026-05-25

---

## 1. Scope

Validates that Adafruit WipperSnapper Arduino firmware correctly interacts with the
FeatherWing OLED 128×32 (SSD1306, I²C address `0x3C`) when stacked on an Adafruit
Feather ESP32-S3.

Specifically this plan verifies:

| # | Capability | Pass condition |
|---|---|---|
| T1 | Firmware build | PlatformIO compiles `adafruit_feather_esp32s3` env without error |
| T2 | Firmware flash | esptool uploads successfully; device resets and connects to WiFi |
| T3 | WipperSnapper checkin | Device sends `checkin.request`; protoMQ responds `R_OK` |
| T4 | Display initialisation | `display.add` command accepted; SSD1306 at `0x3C` responds without I²C error |
| T5 | Message write — first | Text appears on OLED; camera snapshot captures visible content |
| T6 | Message write — repeat | Second write succeeds without driver hang or I²C fault |
| T7 | Pytest pass | `tests/display/` pytest suite exits 0 |

**Out of scope (v1):** display pixel-level correctness, rotation, brightness, power
sleep, OTA update.

---

## 2. Hardware Setup

```
rpi-displays (192.168.1.234)
│
├── USB-A → USB-C  ──►  Feather ESP32-S3
│                        │
│                        └── I²C (GPIO3 SDA / GPIO4 SCL)
│                              └── FeatherWing OLED 128×32 (SSD1306, 0x3C)
│
└── CSI ribbon  ──►  CSI camera (csi-rpi-displays)
                      (points at display bench;
                       captures OLED output for T5/T6)
```

Serial port of the Feather on `rpi-displays`: `/dev/ttyACM0` (adjust `SERIAL_PORT`
if a different port is allocated by the OS — check `ls /dev/ttyACM*` or
`ls /dev/ttyUSB*` on the host).

---

## 3. Prerequisites

| Requirement | Notes |
|---|---|
| HIL controller running | `$HIL_API_BASE` reachable |
| `HIL_API_TOKEN` | Bearer token with `pool=public`, `profile=bench-protomq` |
| `jq` ≥ 1.6 | Used by the submission script |
| `curl` | Used by `examples/hil-call.sh` |
| WiFi credentials | `WIFI_SSID` / `WIFI_PASSWORD` for the bench network |
| protoMQ broker running on `rpi-displays` | Port 1884 (MQTT) and 5173 (API) |
| `rpi-displays` has `platformio` installable | `pip` + network access to PlatformIO registry |
| Feather ESP32-S3 plugged into `rpi-displays` USB | USB-C cable providing data (not charge-only) |
| FeatherWing OLED 128×32 stacked on the Feather | I²C address jumpers: default `0x3C` |

---

## 4. Test Procedure

This is a **single HIL job** that executes all phases in sequence on the
`rpi-displays` worker host.  Each phase maps to a section of the setup/run
command embedded in the job JSON.

### 4.1  Quick start — CLI

```bash
export HIL_API_TOKEN="your-bearer-token"
export WIFI_SSID="bench-wifi"
export WIFI_PASSWORD="bench-password"

# Optional overrides (defaults shown):
# export HIL_API_BASE="http://localhost:8080"
# export DEVICE_ID="display-2900"
# export WIPPERSNAPPER_REF="main"
# export PROTOMQ_REF="main"
# export SERIAL_PORT="/dev/ttyACM0"
# export MQTT_HOST="rpi-displays.local"

bash scripts/submit-arduino-display-test.sh
```

Exit codes from the script (inherited from `examples/hil-call.sh`):

| Code | Meaning |
|------|---------|
| 0 | Job finished — result `pass` |
| 1 | Job finished — result `fail` |
| 2 | Job ended in `error` state |
| 3 | Timeout (local budget or job) |
| 4 | Job was cancelled |

---

### 4.2  Phase-by-phase — raw API calls

Use these when you need to inspect each phase individually or when the
submission script is not available.

#### Phase 1 — Submit the job

```bash
HIL_API_BASE="http://localhost:8080"
HIL_API_TOKEN="your-token"
DEVICE_ID="display-2900"
WS_REF="main"
PROTO_REF="main"
MQTT_HOST="rpi-displays.local"
SERIAL_PORT="/dev/ttyACM0"
WIFI_SSID="bench-wifi"
WIFI_PASSWORD="bench-password"

# Build the job body inline (no jq dependency):
JOB_BODY=$(cat <<EOF
{
  "target": {"device": {"id": "${DEVICE_ID}"}, "pool": "public"},
  "script": "pytest-suite",
  "payload": {
    "kind": "git-source",
    "source": {
      "repo": "https://github.com/adafruit/Adafruit_WipperSnapper_Arduino.git",
      "ref": "${WS_REF}",
      "shallow": true,
      "setup": ["bash", "-c",
        "git clone --depth 1 --branch ${PROTO_REF} https://github.com/tyeth/protomq.git protomq && pip install platformio && pio run -e adafruit_feather_esp32s3 && pio run -e adafruit_feather_esp32s3 --target upload --upload-port ${SERIAL_PORT} && pip install -e . && pip install -e protomq/"
      ]
    }
  },
  "params": {
    "entry": "bash",
    "args": ["-c", "python -m pytest tests/display/ -v --tb=short"],
    "secrets_format": "dotenv",
    "protomq": {
      "broker_host": "${MQTT_HOST}",
      "mqtt_port": 1884,
      "api_port": 5173,
      "script": "feather-s3-ssd1306-128x32-demo"
    }
  },
  "secrets": {
    "MQTT_HOST": "${MQTT_HOST}",
    "MQTT_PORT": "1884",
    "HIL_PROTOMQ_HOST": "${MQTT_HOST}",
    "HIL_PROTOMQ_PORT": "1884",
    "HIL_WIFI_SSID": "${WIFI_SSID}",
    "HIL_WIFI_PASSWORD": "${WIFI_PASSWORD}"
  },
  "secrets_profile": "bench-protomq",
  "timeouts": {"total_s": 1200, "deploy_s": 900, "run_s": 300, "flash_s": 300}
}
EOF
)

RESPONSE=$(curl --fail-with-body --silent --show-error \
    -X POST "${HIL_API_BASE}/v1/jobs" \
    -H "Authorization: Bearer ${HIL_API_TOKEN}" \
    -H "Content-Type: application/json" \
    --data "${JOB_BODY}")

JOB_ID=$(echo "$RESPONSE" | jq -r '.id')
echo "Job submitted: ${JOB_ID}"
echo "Poll URL: ${HIL_API_BASE}/v1/jobs/${JOB_ID}/wait"
```

#### Phase 2 — Long-poll for completion

```bash
SINCE=0
while true; do
    CHUNK=$(curl --fail-with-body --silent \
        "${HIL_API_BASE}/v1/jobs/${JOB_ID}/wait?since=${SINCE}&timeout=300" \
        -H "Authorization: Bearer ${HIL_API_TOKEN}")

    # Stream log events to stdout
    echo "$CHUNK" | jq -r \
        '.events[]? | select(.kind == "log") | "[\(.at)] \(.payload.stream): \(.payload.msg)"'

    SINCE=$(echo "$CHUNK" | jq -r '.next_since')
    STATE=$(echo "$CHUNK" | jq -r '.state')

    case "$STATE" in
        finished) echo "Result: $(echo "$CHUNK" | jq -r '.result')"; break ;;
        error|timeout|cancelled) echo "Job ended: $STATE"; break ;;
    esac
done
```

#### Phase 3 — Retrieve full event log

```bash
# Dump all events as newline-delimited JSON for archiving:
curl --silent \
    "${HIL_API_BASE}/v1/jobs/${JOB_ID}/wait?since=-1&timeout=1" \
    -H "Authorization: Bearer ${HIL_API_TOKEN}" \
    | jq -c '.events[]' > "logs/display-2900-${JOB_ID}.jsonl"

echo "Log saved to logs/display-2900-${JOB_ID}.jsonl"
```

#### Phase 4 — Camera snapshot

```bash
# Capture a JPEG from the display camera while the test is running
# (or immediately after — the last message remains on the OLED):
curl --silent \
    "${HIL_API_BASE}/v1/cameras/csi-rpi-displays/snapshot" \
    -H "Authorization: Bearer ${HIL_API_TOKEN}" \
    --output "logs/display-2900-${JOB_ID}-snapshot.jpg"

echo "Snapshot saved to logs/display-2900-${JOB_ID}-snapshot.jpg"
```

---

### 4.3  UI walkthrough (manual)

1. Open `http://localhost:8080/ui/login` and log in with the bench token.
2. Navigate to **Jobs → + Arduino WS Test**.
3. Fill in the form:
   - **WipperSnapper Arduino ref**: `main` (or a specific commit/branch)
   - **protoMQ ref**: `main`
   - **Play-Script**: `feather-s3-ssd1306-128x32-demo`
   - **Target Device**: `display-2900`
   - **Extra setup**: *(leave as pre-filled default — PlatformIO install + build + flash)*
   - **Test command**: `python -m pytest tests/display/ -v --tb=short`
   - **MQTT Host**: `rpi-displays.local`
4. Click **Submit Job** — the page redirects to the live log view.
5. Confirm each phase succeeds in the log stream (see §5 below).

---

## 5. What the Worker Executes (job internals)

```
setup phase (deploy_s = 900 s budget)
├── git clone --depth 1 --branch {PROTO_REF} .../protomq.git protomq
├── pip install platformio
├── pio run -e adafruit_feather_esp32s3          ← T1: build
├── pio run -e adafruit_feather_esp32s3 \
│     --target upload --upload-port /dev/ttyACM0 ← T2: flash
├── pip install -e .
└── pip install -e protomq/

run phase (run_s = 300 s budget)
├── [protoMQ observer starts, connects to broker]
├── python -m pytest tests/display/ -v --tb=short ← T7
│   └── device boots, connects to WiFi, connects to MQTT
│       ├── checkin.request  →  R_OK              ← T3
│       ├── display.add (SSD1306 @ 0x3C)           ← T4
│       ├── display.write "Hello from ProtoMQ!"    ← T5
│       ├── display.write "Feather ESP32-S3 …"     ← T5
│       ├── display.write "SSD1306 @ 0x3C …"       ← T6
│       └── display.write "PASS: OLED OK …"        ← T6
└── [protoMQ observer teardown; completed steps emitted to job log]
```

The protoMQ script (`feather-s3-ssd1306-128x32-demo`) is driven by the
protoMQ observer inside the worker.  It responds to the device's checkin and
then sends the display commands.  All MQTT messages appear in the job log
as `protomq`-stream events.

---

## 6. Pass / Fail Criteria

| Test | Pass | Fail |
|------|------|------|
| T1 — build | `pio run` exits 0; `.pio/build/adafruit_feather_esp32s3/firmware.bin` present | Compiler errors; missing library |
| T2 — flash | esptool exits 0; `Leaving… Hard resetting via RTS pin…` in log | Upload port not found; CRC mismatch |
| T3 — checkin | protoMQ log shows `checkin.request` received and `R_OK` sent | Device never connects within 60 s of reset |
| T4 — display add | protoMQ log shows `display.addedOrReplaced` event from device | I²C error (address not found, `0x3C` absent) |
| T5 — first write | Camera snapshot shows text on OLED; protoMQ step `write-hello` completes | Camera shows blank/white screen; or step timeout |
| T6 — repeat write | `write-update` and `write-status` steps complete without error | Driver hang; I²C bus stall; step timeout |
| T7 — pytest | `pytest` exits 0; zero `FAILED` in output | Any `FAILED` or `ERROR` test item |

**Job result mapping:**

| pytest exit | protoMQ steps | HIL job result |
|-------------|--------------|----------------|
| 0 (all pass) | all complete | `pass` |
| 1 (test failures) | any | `fail` |
| non-zero (crash) | any | `error` |
| any | any step timeout | `error` or `timeout` |

---

## 7. Log Artifacts

All artifacts should be stored alongside the job ID for traceability.

| Artifact | How to collect | Contents |
|---|---|---|
| Job event log | API `GET /v1/jobs/{id}/wait?since=-1` | MQTT messages, pytest output, state transitions |
| Camera snapshot | API `GET /v1/cameras/csi-rpi-displays/snapshot` | JPEG showing OLED at end of test |
| PlatformIO build log | In job `log` events, `stream=stdout` | Compiler output, library versions |
| pytest XML report | Add `--junitxml=/tmp/results.xml` to test command | Per-test pass/fail times (for CI parsing) |

Suggested directory structure:

```
logs/
  display-2900-{job_id}.jsonl        # full event log
  display-2900-{job_id}-snapshot.jpg # camera capture
  display-2900-{job_id}-junit.xml    # pytest XML (if configured)
```

---

## 8. Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| `pio: command not found` | PlatformIO not installed on worker | Confirm `pip install platformio` ran; check `$PATH` |
| `No such file or directory: /dev/ttyACM0` | Wrong serial port | SSH to `rpi-displays`, run `ls /dev/ttyACM* /dev/ttyUSB*`, set `SERIAL_PORT` |
| `A fatal error occurred: Failed to connect` | Device not in bootloader | Press BOOT+RESET on the Feather before flashing; check USB cable |
| `OSError: [Errno 121] Remote I/O error` | OLED not at `0x3C` | Check FeatherWing address jumpers; run `i2cdetect -y 1` on the host MCU |
| checkin timeout | WiFi credentials wrong | Verify `HIL_WIFI_SSID` / `HIL_WIFI_PASSWORD` in secrets; check serial monitor |
| Blank camera image | Camera angle / focus | Adjust `csi-rpi-displays` ROI in the HIL UI (Devices → display-2900 → Camera Panel) |
| protoMQ step timeout | MQTT broker not running | `systemctl status protomq` on `rpi-displays`; check port 1884 |

---

## 9. Files

| File | Description |
|---|---|
| `vendor/protomq/scripts/feather-s3-ssd1306-128x32-demo.json` | protoMQ play script — checkin + 4 display writes |
| `examples/arduino-display/job.json` | HIL job template (placeholder tokens) |
| `scripts/submit-arduino-display-test.sh` | CLI submission script (uses `jq` + `hil-call.sh`) |
| `vendor/wippersnapper-arduino/platformio.ini` | `[env:adafruit_feather_esp32s3]` target definition |
| `examples/wippersnapper-arduino/secrets.example.json` | Secrets template — WiFi + MQTT for the device |
| `examples/hil-call.sh` | Long-poll helper (shared with Python variant) |

---

## 10. Future Iterations

In v1 each phase is a separate `curl` call or a single `bash` submission script.
Planned improvements:

- **v2 — pytest orchestration**: a single `tests/test_display_oled_128x32.py` that
  calls the HIL job API, waits for completion, and asserts on the result — replacing
  the shell script with a proper pytest test case that can be run from CI.

- **v3 — parameterised matrix**: extend to all OLED products in the topology
  (`display-2900`, `display-4440`, `display-4650`) using pytest parametrize, one
  protoMQ script per product.

- **v4 — pixel verification**: compare camera snapshot against a reference image
  using the `CameraCapture` / QR-locator pipeline to verify exact text content
  rather than just "something is visible".
