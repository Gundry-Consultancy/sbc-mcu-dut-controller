---
name: hil-ws-telemetry
description: "Prove a WipperSnapper v2 telemetry component (boot_reason, RSSI, …) end-to-end on the bench — flash a WS build, stand up protomq with the telemetry proto, inject a ws.telemetry Add over the broker, and capture the decoded ws.telemetry Event (D2B) plus device serial. Builds on hil-job-api. Use when validating telemetry.proto or the firmware telemetry component against real hardware."
---

# hil-ws-telemetry

Proves the WipperSnapper **telemetry** API on hardware: the broker adds a telemetry
metric identified by the `ws.telemetry.Type` enum, the device reads it and publishes a
`ws.telemetry.Event` (D2B). Foundation: **hil-job-api** (reach the controller, targets,
firmware upload, `POST /v1/jobs`, `wait`, assets, stage vocabulary) — this skill only adds
the telemetry-specific parts.

## 1. protomq must speak telemetry
`telemetry.proto` lands on Protobuf `api-v2` when its PR merges; until then it lives on the
feature branch. Pass the proto ref in the job so the per-job protomq clone imports it:

```jsonc
"params": { "protobuf_ref": "<telemetry-proto-branch>", ... }   // default is api-v2
```

The launcher runs `npm run import-protos && npm run build-web` automatically, so protomq
will encode/decode `ws.telemetry`. No persistent bench edit is needed — protomq is cloned
+ built fresh per job (see hil-job-api / `protomq_launcher`).

## 2. Stage recipe (ESP32, retained-secrets path)

```
enter_bootloader, flash, launch_protomq, power_cycle, verify_checkin, inject_protobuf, print_boot_log
```

- **Avoid `write_secrets_msc` on native-USB boards** (QT Py S3, etc.): the USB-MSC volume
  frequently fails to enumerate and the stage errors before inject. protomq binds a stable
  MQTT port, so a board that has checked in once retains valid secrets across re-flashes
  (flashing app@`0x10000` leaves the filesystem partition intact). Drop the stage and the
  board checks in on its retained secrets. Only include it for a first-time board (and
  expect MSC-enumeration flakiness — retry, or pre-warm with a longer power-off hold).

## 3. inject_protobuf — two things you MUST pass

`inject_protobuf` publishes a raw `ws.signal.BrokerToDevice` (`payload_hex`) to
`<io_user>/ws-b2d/<uid>`. Two controller quirks (worth fixing upstream) to work around:

- Its default MQTT client dials `ctx.protomq_host`, which may be a **non-resolvable
  hostname** (`gaierror: Name or service not known`) → the stage dies with
  `no DUT checkin observed`. **Pass `protomq_api_url: "http://127.0.0.1:5173"`** so it
  publishes via protomq's localhost HTTP `/api/echo` instead.
- **Pass `topic` (or `uid`) explicitly** to skip its own `wait_for_checkin`
  (`verify_checkin` already confirmed the DUT is up).

```jsonc
{ "type": "inject_protobuf",
  "payload_hex": "ca02040a020801",              // BrokerToDevice{telemetry: B2D{add:{type:TM_BOOT_REASON}}}
  "topic": "<io_user>/ws-b2d/<uid>",             // e.g. hil/ws-b2d/qtpy-esp32s3-n4r2<macUID>
  "protomq_api_url": "http://127.0.0.1:5173",
  "settle_s": 12 }
```

## 4. Encoding a telemetry B2D

`ws.signal.BrokerToDevice.telemetry = 41` → `ws.telemetry.B2D{add|remove}` →
`Add{type: ws.telemetry.Type, period: float}` (D2B telemetry = field 22). Encode with
`protoc` against the proto checkout (robust; avoids hand-encoding):

```bash
printf 'telemetry { add { type: TM_BOOT_REASON period: 0 } }' \
  | protoc -Iproto/wippersnapper -Inanopb/generator/proto \
      --encode=ws.signal.BrokerToDevice proto/wippersnapper/signal.proto | xxd -p
```

- `TM_BOOT_REASON` (report-once, `T_BYTES`) → **`ca02040a020801`**
- `TM_RSSI = 10` (periodic, `T_RAW` float) → `telemetry { add { type: TM_RSSI period: 300 } }`

## 5. Expected result (the proof)

Device serial: `[telemetry] Handle_TelemetryAdd: <name>` → `New metric added!`.
protomq decodes the D2B on `<io_user>/ws-d2b/<uid>`:

```json
{"telemetry":{"event":{"type":"TM_BOOT_REASON","value":{"type":"T_BYTES",
  "bytesValue":"CPU0: POWERON_RESET\nCPU1: POWERON_RESET"}}}}
```

boot_reason is **per-CPU-core** on multi-core ESP32. Pull `serial.log` + `protomq.log`
from `GET /v1/jobs/{id}/assets` as the proof artifacts.

---
**Verified** on a QT Py ESP32-S3 DUT with the WipperSnapper telemetry firmware
(adafruit/Adafruit_Wippersnapper_Arduino#946) and protomq's telemetry proto ref
(adafruit/Wippersnapper_Protobuf#215): full round-trip checkin `R_OK` → inject
`TM_BOOT_REASON` → device published `CPU0: POWERON_RESET\nCPU1: POWERON_RESET`.
