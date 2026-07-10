# HIL controller HTTP API

The controller exposes a small HTTP API (FastAPI) that a test script or CI job
uses to run firmware on real hardware and pull back proof. This documents the
endpoints a **test author** needs; the web UI and inventory/topology endpoints
are covered in [`ARCHITECTURE.md`](ARCHITECTURE.md).

Base URL (over Tailscale): `http://tachyon-16ee27b8.ostrich-escalator.ts.net:8080`
(`192.168.1.169:8080` on the LAN). All `/v1/*` calls require a bearer token:

```
Authorization: Bearer $HIL_API_TOKEN
```

> **The controller fills in bench-specific values** so callers stay
> device-agnostic: the protomq broker host:port (written into the DUT's
> `secrets.json`) and the MSC `msc_filter` (derived from the device's by-path
> serial) are supplied controller-side — a caller never passes them. Same idea
> as device wiring: `serial_port` / `hub` / `solenoid_channel` come from the DB,
> not the request.

---

## Quick path: run a firmware test

```
POST /v1/firmware?filename=fw.combined.bin   (raw body)   → { id, path, sha256, ... }
POST /v1/jobs                                 (job JSON)   → { id }
GET  /v1/jobs/{id}/wait?since=N&timeout=10                 → { events, next_since, state }
GET  /v1/jobs/{id}/assets                                  → { assets: [...] }
GET  /v1/jobs/{id}/assets/{asset_id}/download             → file bytes
```

---

## Endpoints

### `GET /v1/targets` — availability matrix

What the bench can run *right now*, keyed by the arduino-cli **build-target**
name so a CI matrix maps 1:1 to its build artifacts. Skipped targets are
reported (never silently dropped). See [`device-availability.md`](device-availability.md).

```json
{ "targets": [
  { "target": "qtpy_esp32s3_n4r2", "device_id": "mcu-…", "available": true,
    "status": "available", "kind": null, "reason": null },
  { "target": "metro_esp32s2", "available": false, "status": "unavailable",
    "kind": "permanent", "reason": "not wired to bench" }
]}
```

### `POST /v1/firmware?filename=<name>` — upload firmware

Raw request body = the `.bin`. Stores it controller-side and records a tracked
`kind='firmware'` asset (purged after `HIL_FIRMWARE_PURGE_DAYS`, default 7).
Returns `{ id, filename, path, size_bytes, sha256 }`. Pass the returned `path`
as `params.firmware.path` in the job. (Alternatively a job may set
`params.firmware.url` + optional `sha256` and the controller downloads it.)

### `POST /v1/jobs` — submit a job

```jsonc
{
  "target": { "device": { "id": "mcu-feather-eink-29-rbw" }, "pool": "public" },
  "script": "firmware-bench",
  "params": {
    "firmware": { "path": "/…/uploads/…combined.bin", "offset": "0x0" },
    "window_minutes": 10,             // interactive hold after the pipeline
    "stages": [ /* see the stage vocabulary below */ ]
    // msc_filter optional — derived from the device serial when omitted
  },
  "secrets": { "IO_USERNAME": "hil", "IO_KEY": "hil",
               "WIFI_SSID": "…", "WIFI_PASSWORD": "…" },
  "timeouts": { "total_s": 1200 }
}
```

`target.device.id` selects a specific device (authoritative); omit it to match
by `pool`/`kind`/`model`/`capabilities`. Returns `202` + `{ id }`.

Device selection enriches the matched device with the DB's authoritative
hardware fields (`serial_port`, `hub_host_id`, `hub_port_path`,
`solenoid_channel`, `flasher`, `build_target`) — topology.yaml is only a seed.

### `GET /v1/jobs/{id}` — snapshot

`{ id, state, result, … }`. `state` ∈ `queued | assigned | preparing |
flashing | running | finished | failed | cancelled | error | timeout`.
(`firmware-bench` runs its whole stage pipeline inside the **`flashing`** phase,
then holds in **`running`** until the window/lease expires.)

### `GET /v1/jobs/{id}/wait?since=<seq>&timeout=<s>` — long-poll events

Returns events with `seq > since`, blocking up to `timeout` s for new ones:

```json
{ "events": [ { "seq": 12, "kind": "log",   "payload": { "stream": "bench", "msg": "…" } },
              { "seq": 13, "kind": "state", "payload": { "state": "running" } } ],
  "next_since": 13, "state": "running" }
```

Poll loop: pass `next_since` back as `since`; stop when `state` is terminal
(`finished | failed | cancelled | error | timeout`). `kind` is `log` or `state`;
log `payload.stream` is `bench` (stage narration), `serial` (DUT serial), or
`protomq` (broker stdout). **Don't burn your poll budget** on the `serial`
stream flood — filter it out, or rely on the captured assets after the run.

### `POST /v1/jobs/{id}/cancel` · `POST /v1/jobs/{id}/extend`

Cancel the running job (cancels the asyncio task, not just a DB flag). `extend`
bumps an interactive hold's lease `expires_at` (body `{ "minutes": N }`).

### `GET /v1/jobs/{id}/assets` · `…/assets/{asset_id}/download`

Lists/streams the run's captured artifacts: `serial.log`, `flash.log`,
`protomq.log` (each line/command **UTC-ms timestamped** so they correlate), plus
the linked `firmware`. CI pulls these as proof instead of scraping the UI.

---

## `firmware-bench` stage vocabulary

`params.stages` is an ordered list of `{ "type": …, …opts }`. Handlers
(`adapters/bench_stages.py: STAGE_HANDLERS`):

| stage | what it does | common opts |
|---|---|---|
| `enter_bootloader` | get the chip into ROM download mode (1200-touch → USB-JTAG reset → hub recovery) | `attempts`, `reset_attempts` |
| `erase` | `esptool erase_flash` | `before`/`after` (default `no_reset`) |
| `flash` | `esptool write_flash <offset>` | `offset`, `path`, `before`/`after` |
| `verify` | `esptool verify_flash` | `offset`, `path` |
| `launch_protomq` | stand up a per-job broker on the controller (auto-inserted before the first `flash` when secrets are written) | — |
| `start_serial_log` | attach serial capture (auto-inserted before the first `power_cycle`) | — |
| `power_cycle` | solenoid cold-boot; **awaits the DUT's USB node disappearing then re-enumerating** (detection, not fixed timers) | `off_s`, `settle_s`, `await_enumeration`, `disappear_timeout_s`, `reappear_timeout_s` |
| `write_secrets_msc` | drop `secrets.json` on the DUT's MSC volume (needs the app booted → run a `power_cycle` first) | `msc_filter` (else derived) |
| `print_boot_log` | dump `wipper_boot_out.txt` from the MSC (auto-inserted after the last `power_cycle`) | — |
| `verify_checkin` | wait for the DUT to check in to the broker; logs `CHECKIN_VERDICT ok=true\|false` (lightweight smoke test, no injection) | `checkin_timeout_s` |
| `inject_pixelwrite` | the #926/#927 regression: fire a v1 pixelWrite, detect crash-vs-graceful; logs `PIXELWRITE_VERDICT rebooted=true\|false` | `pin`, `color`, `checkin_timeout_s`, `observe_s` |
| `inject_protobuf` | publish any `ws.signal.BrokerToDevice` to the DUT over the broker (protoMQ `POST /api/echo` on `<io_user>/ws-b2d/<uid>`); logs `INJECT_VERDICT published=true`. Use a convenience `kind` builder (e.g. `display_add_i8080`) or raw `payload_hex` | `kind`, `params`, `payload_hex`, `uid`, `settle_s` |
| `inject_i2c_probe` | fire a v2 I2C `Probe` (bare bus or one mux channel) at a checked-in DUT and capture the reply; logs `I2C_PROBE_VERDICT scan=… found=[0x..]` per scan | `pin_scl`, `pin_sda`, `addresses`, `mux_address`, `mux_channel` |
| `inject_i2c_scan_v1` | v1-style full I2C scan; logs `I2C_SCAN_VERDICT port=<n> found=[0x..]` per port | `port` |
| `inject_i2c_settings` | inject a component add + settings and read back its events; logs `I2C_SETTINGS_VERDICT label=… status=ok\|error\|no_event readings={…}` | `settings`, `label` |
| `select_i2c_strand` / `isolate_i2c_strand` | route a shared I2C component strand to **this** DUT via the ADG729 analog strand-mux (break-before-make; auto-prepended when `target.requires` names components), or isolate it; logs `I2C_STRAND_MUX_VERDICT` | `strand`, `group`, `channel` |
| `capture_display` | photograph the DUT panel via the camera server (+ ROI crop), registered as a job asset; logs `DISPLAY_CAPTURE_VERDICT` | `camera_url`, `roi`, exposure/focus opts |
| `bootloader_touch` | 1200-baud touch to flip a running app into ROM download (lighter than `enter_bootloader`) | — |
| `diagnose` | classify a stuck boot state | — |

`launch_protomq` / `start_serial_log` / `print_boot_log` are inserted
automatically at the right moments unless you place them explicitly.

**v2 broker wire contract.** The injection stages talk to the DUT over the
per-job protoMQ broker on two per-device topics: `<io_user>/ws-b2d/<uid>`
(broker → device, a `ws.signal.BrokerToDevice`) and `<io_user>/ws-d2b/<uid>`
(device → broker, a `ws.signal.DeviceToBroker`). `inject_protobuf` publishes via
protoMQ's `POST /api/echo` (the payload is the encoded protobuf sent as a
**latin1** string). The message *shape* — oneof members and field numbers — is
defined by the current [`Wippersnapper_Protobuf`](https://github.com/adafruit/Wippersnapper_Protobuf)
`.proto` and the firmware's own nanopb headers; treat those as ground truth
(they move as the protos evolve). The encoders in `adapters/ws_*_inject.py`
follow the headers and are updated when the proto changes — so don't rely on a
field number written down in prose. protoMQ's own HTTP control API
(`/api/echo`, `/api/autoresponse`, `/api/scripts/*`) is documented in its README.

**Verdict detection** (`inject_pixelwrite`): a crash resets the chip within ~1–2s
(seen in the serial log immediately) but the device can't re-checkin over MQTT
until WiFi+broker reconnect (15–40s). The stage races a **serial reset-banner**
watcher against the **MQTT re-checkin** and takes whichever proves a reboot
first — so a real crash isn't misreported as "survived". The grep-able verdict
line is the contract; the stage itself never pass/fails on the reboot (the harness
compares the two builds).

---

## Timeouts

| knob | where | default | purpose |
|---|---|---|---|
| `timeouts.total_s` | job | 1800 (non-interactive) | hard ceiling; **not** applied to `firmware-bench` (interactive — its lease owns the deadline) |
| `params.window_minutes` | job | 10 | how long `firmware-bench` holds the device after the pipeline |
| `checkin_timeout_s` | stage | 120 | wait for the DUT to check in |
| `observe_s` | `inject_pixelwrite` | 30 | watch window for a reboot after firing |
| `disappear/reappear_timeout_s` | `power_cycle` | 10 / 30 | await USB node vanish / re-enumerate |

A generous job-level deadline (≈30 min) is deliberate so a *slow* reboot/checkin
isn't missed; tighten it (≈5 min) for quick smoke tests that neither build
firmware nor wait on a long reboot. Where possible, run validation **after** the
capture has finished rather than racing the live stream — unless the DUT-host
itself runs the suite (rather than a CI script grepping log output).

---

## Cameras & ROIs

The controller proxies bench cameras and can crop a per-device region of interest
(ROI) out of a frame — e.g. a DUT's display for visual checks.

### ROIs are frame-relative

An ROI is stored as pixel coords `(x, y, w, h)` **plus the frame size it was drawn
on** (`roi_frame_width`, `roi_frame_height`). Consumers scale every coord by
`actual_capture_dims / roi_frame_dims`, so the same ROI is valid against any
resolution. This matters because the bench Pi camera-server serves two sizes: a
fast **warm** frame (e.g. `2328×1748`) and a sensor-native **full** still (e.g.
`4656×3496`, exactly 2× here). An ROI drawn on the warm frame is auto-scaled when
you ask for the full-res crop. Legacy ROIs with no recorded frame size are
back-filled (from the warm frame) the first time a full-res crop is requested.

### Endpoints

| method | path | purpose |
|---|---|---|
| `GET` | `/v1/cameras` · `/v1/cameras/{id}` | list / detail (now includes `resolution_w/h`) |
| `GET` | `/v1/cameras/{id}/snapshot` | one JPEG from the camera's primary stream |
| `GET` | `/v1/devices/{id}/camera` | camera assignment + ROI (incl. `roi_frame_width/height`) |
| `PUT` | `/v1/devices/{id}/camera/roi` | set a manual ROI |
| `DELETE` | `/v1/devices/{id}/camera/roi` | clear the ROI |
| `GET` | `/v1/devices/{id}/camera/snapshot?res=&pad=` | ROI-cropped frame |
| `POST` | `/v1/devices/{id}/camera/calibrate` · `…/calibrate/save` | QR auto-detect ROI (propose / save) |

**`PUT …/camera/roi`** body: `{x, y, w, h, frame_width?, frame_height?}`. Omit
`frame_width/height` and the controller detects them from a live snapshot so the
ROI stays scalable. Calibrate/save records the frame it detected on automatically.

**`GET …/camera/snapshot`** query params:
- `res=warm` (default) — crop the fast warm frame (back-compatible).
- `res=full` — crop the sensor-native still and scale the ROI from
  `roi_frame_*`. Far sharper; ~1–2 s on a Pi (reconfigures to still mode).
- `pad=<0..2>` — grow the ROI box by this fraction on each side (default 0).

```bash
# Sharp crop of a DUT's display, straight from the API:
curl -H "Authorization: Bearer $TOK" \
  "$BASE/v1/devices/mcu-feather-eink-29-rbw/camera/snapshot?res=full&pad=0.05" \
  -o eink.jpg
```

> Be gentle with `res=full` on weak hosts (e.g. a Pi Zero 2 W): the 16 MP
> still-mode reconfigure is heavy and wedges if hammered. Capture one-shot — don't
> poll it in a tight loop.
