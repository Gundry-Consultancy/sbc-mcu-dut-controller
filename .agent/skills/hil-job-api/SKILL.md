---
name: hil-job-api
description: "Foundation reference for driving the HIL controller's HTTP job API — discover available bench targets (GET /v1/targets), upload firmware, submit jobs (POST /v1/jobs with script=firmware-bench|pytest-suite), long-poll events (GET /v1/jobs/{id}/wait), and pull proof assets (GET /v1/jobs/{id}/assets) — plus auth, pools, target.requires (i2c strands), the current stage vocabulary, and the GitHub-Actions job-runner pattern downstream repos copy. Target-app agnostic: works for any firmware/app the bench can flash and observe. Use FIRST when interacting with the controller; the task-shaped skills (hil-author-test, hil-firmware-compare, hil-bisect, hil-display-pytest) build on this and link back here instead of restating it."
---

# hil-job-api

Everything on the bench is reached through one small HTTP API. This skill is
the shared vocabulary; the task-shaped skills reference it.

## Reaching the controller

- Base URL: `http://192.168.1.169:8080` on the bench LAN, or
  `http://tachyon-16ee27b8.ostrich-escalator.ts.net:8080` over Tailscale.
  CI passes it as `HIL_API_BASE`.
- Every `/v1/*` call: `Authorization: Bearer $HIL_API_TOKEN`. Tokens are static
  bootstrap strings or argon2-hashed minted ones (`scripts/mint-token.py`);
  the dev bench default is `dev-token-change-me`.
- The web UI (same port, cookie `hil_token`) is the human-validated mirror of
  everything below — job forms, live logs, camera calibration, solenoid power.

## The core loop

```
GET  /v1/targets                                  → what can run right now
POST /v1/firmware?filename=fw.combined.bin        → { path, sha256, … }   (raw body = the .bin)
POST /v1/jobs                                     → 202 { id }
GET  /v1/jobs/{id}/wait?since=N&timeout=10        → { events, next_since, state }
GET  /v1/jobs/{id}/assets                         → { assets: [serial.log, protomq.log, …] }
GET  /v1/jobs/{id}/assets/{asset_id}/download     → file bytes (CI proof)
```

`GET /v1/targets` is keyed by arduino-cli **build-target** name so a CI build
matrix maps 1:1 to bench devices. Unavailable targets are *reported with a
reason*, never silently dropped — treat `kind: temporary` as retry-later and
`kind: permanent` as skip (see hil-bench-recovery for the availability model).

## Job request shape

```jsonc
{
  "target": {
    "device": { "id": "mcu-qtpy-esp32s3-n4r2-hil006" },   // authoritative; or match by pool/model/capabilities
    "requires": [                                          // optional aux requirements
      { "kind": "i2c_strand", "capabilities": ["sensor:pm25"] }
    ],
    "pool": "public"
  },
  "script": "firmware-bench",          // or "pytest-suite" (SBC/python path)
  "params": {
    "firmware": { "path": "/…/uploads/….bin", "offset": "0x0" },  // or { "url": …, "sha256": … }
    "window_minutes": 10,              // interactive hold after the pipeline
    "stages": [ /* ordered stage list — see vocabulary */ ]
  },
  "secrets": { "IO_USERNAME": "hil", "IO_KEY": "hil",
               "WIFI_SSID": "…", "WIFI_PASSWORD": "…" },
  "timeouts": { "total_s": 1200 }
}
```

The controller fills in bench-specific values so callers stay device-agnostic:
broker host:port (written into the DUT's `secrets.json`), `msc_filter` (derived
from the device's by-path serial), and the device's wiring (`serial_port`,
`hub_host_id`, `solenoid_channel`, `flasher`, `build_target`) all come from the
DB, never the request.

**`target.requires` with `kind: i2c_strand`** matches a strand by component
capabilities or model short-names (case-insensitive) and **auto-prepends a
`select_i2c_strand` stage** routing that strand's shared I2C bus to the matched
DUT. See hil-i2c-strands for the strand model.

## Stage vocabulary (current registry)

`params.stages` entries, in the order you want them run
(`src/hil_controller/adapters/bench_stages.py: STAGE_HANDLERS`):

| stage | purpose |
|---|---|
| `enter_bootloader` / `bootloader_touch` | get the chip into ROM download mode (1200-touch → USB-JTAG reset → hub recovery) |
| `erase` / `flash` / `verify` | esptool ops; keep `before`/`after: no_reset` between them on native-USB parts |
| `power_cycle` | solenoid cold-boot, awaiting USB disappear/re-enumerate; `reset_via: esptool` does a soft reset **without unmapping the solenoid** (preserves latched I2C muxes) |
| `write_secrets_msc` | drop `secrets.json` on the DUT's MSC volume — the app must be booted first, so a `power_cycle` precedes it |
| `verify_checkin` | wait for broker check-in; logs `CHECKIN_VERDICT ok=true|false` (v1 and v2 topic aware) |
| `launch_protomq` / `start_serial_log` / `print_boot_log` | auto-inserted at the right moments unless placed explicitly |
| `inject_protobuf` | publish any BrokerToDevice message via the broker's `/api/echo` — the generic driver for poking the app under test |
| `inject_i2c_probe` / `inject_i2c_scan_v1` | I2C presence checks over the broker |
| `inject_i2c_settings` | Add-with-settings injector for I2C components (custom per-driver settings) |
| `inject_pixelwrite` | the #926/#927 crash-vs-graceful example; logs `PIXELWRITE_VERDICT rebooted=true|false` |
| `select_i2c_strand` / `isolate_i2c_strand` | route / disconnect a shared component strand (hil-i2c-strands) |
| `capture_display` | autofocus + manual-exposure camera grab + ROI crop → job asset (hil-camera-proof) |
| `diagnose` | classify a stuck boot state |

The proven ESP32 combined-bin order (and why) lives in **hil-author-test**;
SAMD/UF2 boards use a bossac/UF2-MSC default cycle instead — both are seeded
from `DEFAULT_FLASH_STAGES` variants in `bench_stages.py`.

## Waiting well

- Long-poll `wait` with `since=next_since`; stop on terminal state
  (`finished | failed | cancelled | error | timeout`).
- Event `kind` is `log` or `state`; log `payload.stream` ∈ `bench` (stage
  narration), `serial` (DUT flood — filter it out live), `protomq`.
- Grep-able verdict lines (`CHECKIN_VERDICT …`, `PIXELWRITE_VERDICT …`) are the
  contract; prefer asserting on downloaded assets after the run over racing the
  live stream.
- `POST /v1/jobs/{id}/extend {"minutes": N}` bumps an interactive hold;
  `POST /v1/jobs/{id}/cancel` really cancels the task.
- `firmware-bench` runs its whole pipeline in the `flashing` state then holds in
  `running` for `window_minutes` — a "stuck in flashing" job is usually just
  mid-pipeline.

## CI: the GitHub job-runner pattern

Downstream repos don't run bench code — they POST jobs. Copy
`.github/workflows/example-hil-call.yml` + `examples/hil-call.sh`:

1. Build firmware in CI (or reference a release asset URL + sha256).
2. `GET /v1/targets`, intersect with the build matrix, **report skipped targets**.
3. Submit per-target jobs, poll `wait`, download assets.
4. Post verdict + proof (log excerpts, camera crops) as a PR comment / job summary.

Auth from CI is a repo-scoped bearer token or GitHub OIDC; network path is
Tailscale (`TAILSCALE_AUTHKEY`) or any route to the controller — only
`HIL_API_BASE` changes. `examples/wippersnapper-arduino/hil-lib.sh` shows the
resilience layer worth copying: `wait_for_target_available` (rides through a
DUT-host reboot) and `is_infra_error` (transient infra ≠ test verdict).

## Cameras in one breath

Per-device ROIs are frame-relative (`x,y,w,h` + the frame size they were drawn
on) and scale automatically between the warm stream and the sensor-native still:
`GET /v1/devices/{id}/camera/snapshot?res=full&pad=0.05` returns a sharp crop of
the DUT's panel. Full detail, calibration and `capture_display` tuning:
**hil-camera-proof**.
