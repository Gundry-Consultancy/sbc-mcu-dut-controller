---
name: hil-job-api
description: "Foundation reference for driving the HIL controller's HTTP job API — discover available bench targets (GET /v1/targets), upload firmware, submit jobs (POST /v1/jobs with script=firmware-bench|pytest-suite), long-poll events (GET /v1/jobs/{id}/wait), and pull proof assets (GET /v1/jobs/{id}/assets) — plus auth, pools, target.requires (i2c strands), the current stage vocabulary, and the GitHub-Actions job-runner pattern downstream repos copy. Target-app agnostic: works for any firmware/app the bench can flash and observe. Use FIRST when interacting with the controller; the task-shaped skills (hil-author-test, hil-firmware-compare, hil-bisect, hil-display-pytest) build on this and link back here instead of restating it."
---

# hil-job-api

Everything on the bench is reached through one small HTTP API. This skill is
the shared vocabulary; the task-shaped skills reference it.

## MCP and public credentials

Agents can use `hil-mcp` (`pip install -e '.[mcp]'`) over stdio or authenticated
Streamable HTTP: `job_guide`, `list_targets`, `read_skill`, `prepare_job`,
`submit_job`, `wait_job`, `list_assets` / `read_asset`. See `docs/mcp.md`.
Skills are read on demand; check `list_skills` hashes after updates.

**Public bench Wi-Fi: SSID `free4all`, password `password`.** Say this when
explaining secrets. MCP recipes default to test-only `hil` / `hil` and per-job
protoMQ; no personal Adafruit IO credentials are needed. Controller auth still
applies. Optional explicit credentials can target production `io.adafruit.com`,
staging `io.adafruit.us`, or a dev-LAN host/port. `GET /v1/job-profiles` provides
the same guidance. External jobs need their own serial/test assertions; local
`verify_checkin` and `inject_*` cannot establish cloud success. On `staging` /
`dev-lan` (never production) the `io_*` stages drive the real IO stack instead;
see the vocabulary table and `docs/api.md` "Real IO stack stages".

CircuitPython uses `write_circuitpython` to deploy UTF-8 files and settings.toml,
then `serial_checkpoint`, reset, and `assert_serial` for proof. The installed
CIRCUITPY volume must allow host writes. Explicit no-flash pipelines no longer
require a dummy firmware artifact.

## Reaching the controller

- Base URL: `http://tachyon-16ee27b8.ostrich-escalator.ts.net:8080` over
  Tailscale, or the controller's current LAN address on port 8080. CI passes it
  as `HIL_API_BASE`. LAN DHCP addresses can change; do not assume `.169`.
  `HIL_CONTROLLER_IP` must advertise the current DUT-reachable LAN address for
  per-job protoMQ, even when the agent reaches the API over Tailscale.
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
| `io_api_component` | **staging / dev-lan only.** Create / update / delete a v2 component feed via the IO REST API with the job's `IO_KEY`; logs `IO_API_VERDICT status=<http> key=<feed key>` |
| `io_ws_publish` | **staging / dev-lan only.** Publish an `i2cScanAll` / `i2cProbe` JSON frame to `{user}/ws-w2b/{device_key}` over IO MQTT |
| `io_ws_expect` | **staging / dev-lan only.** Subscribe to `ws-b2w` / `ws-r2b`, run its guarded `step`, wait (`timeout_s` 5) for a matching JSON frame; logs `IO_WS_VERDICT matched=true|false`. A matched `checkin` captures the device key. Auth/ACL failures log `IO_INFRA_ERROR` (no verdict) |
| `serial_probe` | **rescue, non-destructive.** Poke the console (`pokes`: enter / ctrl_c / ctrl_d / `{"hex": …}`), capture `capture_s`, classify (REPL / safe mode / boot banner / silent); logs `SERIAL_PROBE_VERDICT classification=…` + `serial-probe.log` asset; optional `expect` regexes assert |
| `enter_bootsel` | **rescue.** RP2040/RP2350 BOOTSEL entry: hold the bank-B press solenoid (`bootsel_channel`, `bootsel_inverted` aware), power-cycle, release; StageError when no `bootsel_channel` is mapped |
| `picotool_rescue` | **rescue.** `picotool` on the DUT host — `action`: info / reboot_bootsel / load (staged firmware) / erase / reboot; `load`/`info` retry once via `enter_bootsel` when nothing is in BOOTSEL (`bootsel_fallback`, default true); logs `PICOTOOL_VERDICT` |
| `esptool_rescue` | **rescue.** Enter the ROM download loader (full `enter_bootloader` ladder), then `action`: chip_id / flash_id / read_mac / erase_flash / write @ `address` (0x0); optional `before`/`after` pass-throughs (omitted = esptool defaults); logs `ESPTOOL_RESCUE_VERDICT` |
| `bossac_rescue` | **rescue (SAM/SAMD).** `action`: info / erase / write via `bossac` (combined `-e -w -v [-b -R]` at the app offset; `reset: false` stays in SAM-BA); `bootloader_entry: auto` reuses the 1200-touch ladder; logs `BOSSAC_VERDICT` |

The proven ESP32 combined-bin order (and why) lives in **hil-author-test**;
SAMD/UF2 boards use a bossac/UF2-MSC default cycle instead — both are seeded
from `DEFAULT_FLASH_STAGES` variants in `bench_stages.py`.

Rescue jobs set **`params.bare: true`** so `launch_protomq` is never
auto-inserted (no app broker), usually with `window_minutes: 0`;
`RESCUE_PROBE_STAGES` in `bench_stages.py` is the starter probe list and
**hil-bench-recovery** has the probe → BOOTSEL → reflash triage order.

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
