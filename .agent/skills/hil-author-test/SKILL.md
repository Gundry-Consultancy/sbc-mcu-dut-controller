---
name: hil-author-test
description: "Author a hardware-in-the-loop (HIL) test script or example against the usbip-hil-controller — flash any firmware on a real bench DUT, drive the running app (power-cycle, secrets, protobuf/I2C injection, strand routing), and assert on captured logs / verdict lines / camera proof. Target-app agnostic; WipperSnapper appears only as a worked example. Use when writing a NEW test/example (a boot/version check, a check-in smoke test, a signal or sensor regression, a custom flash sequence for ESP32/RP2040/SAMD) or a CI step that runs one. Covers the proven stage ORDER per chip family, verdict contract lines, timeouts, and the CI test-array pattern. NOT for: the two-build A/B regression pattern (see hil-firmware-compare); endpoint/auth/job-shape mechanics (see hil-job-api); a single ad-hoc manual flash (submit a firmware-bench job directly)."
---

# hil-author-test

How to write a HIL test against the controller. A test = **flash a firmware on a
real DUT, bring it to a known state, do something, and assert on what the
hardware reported** (serial / broker / esptool logs, optionally a camera crop).
The controller's `firmware-bench` script runs an ordered **stage pipeline**; you
compose stages and assert on machine-greppable verdict lines. Nothing here is
tied to one firmware — any app the bench can flash and observe fits.

Endpoints, auth, the job request shape, and the full stage table live in
**hil-job-api** (and [`docs/api.md`](../../docs/api.md)); this skill is the
*authoring* guide on top of them.

## The shape of every test

1. **Pick a target** — `GET /v1/targets` → run only `available` ones, skip +
   report the rest (temporary → give the controller its ≤3-try/~3-min heal
   budget; permanent → skip now). Never simulate hardware.
2. **Get firmware onto the controller** — `POST /v1/firmware?filename=…` (raw
   body) and use the returned `path`, or set `params.firmware.url` and let the
   controller download it.
3. **Submit a `firmware-bench` job** — `POST /v1/jobs` with a stage pipeline,
   `secrets`, and timeouts (below). Job shape: see hil-job-api.
4. **Wait** — long-poll `GET /v1/jobs/{id}/wait?since=&timeout=`; stop on a
   terminal `state` (`finished|failed|cancelled|error|timeout`). Filter out the
   `serial` log stream so it doesn't drown your poll.
5. **Pull proof** — `GET /v1/jobs/{id}/assets` → download `serial.log` /
   `flash.log` / `protomq.log` (all UTC-ms timestamped) + the `boot_out.txt`
   version. Prefer asserting on the **finished** captured logs over racing the
   live stream — unless the DUT-host itself runs the assertion.

## The proven stage order (don't reinvent it)

### ESP32 combined.bin (esptool)

```
enter_bootloader            # 1200-touch → USB-JTAG reset → hub recovery
erase                       # before/after: no_reset
flash        @0x0           # before/after: no_reset
(verify)                    # optional, also before/after: no_reset
power_cycle                 # cold-boot into the app
<drive + assert stages>     # whatever your app/test needs (see below)
```

Critical gotchas baked into this order:
- Keep `before`/`after: no_reset` on every esptool op between `enter_bootloader`
  and the `power_cycle` — a native-USB part drops off the bus if the ROM stub
  resets between steps.
- `launch_protomq` (before first flash), `start_serial_log` (before first
  power_cycle), and `print_boot_log` (after last power_cycle) are **auto-inserted**
  — don't add them unless you need explicit placement.
- **Exception — no-flash / no-secrets pipelines** (re-driving an already-flashed
  board: `power_cycle` + `verify_checkin`/injects only): with no secrets stage the
  broker is NOT auto-launched ("no secrets stage / launch_protomq → protomq will
  not be launched") and `verify_checkin` errors with "needs protomq running".
  Add an explicit `{"type": "launch_protomq"}` first. `params.firmware` is still
  required by the adapter even though nothing flashes it (verified live 2026-07-02).
- The controller **fills bench specifics itself**: the protomq broker host:port
  written into the DUT's secrets, the `msc_filter` (derived from the device's
  by-path serial), and the device wiring (`serial_port`, `solenoid_channel`,
  `flasher`, …). Don't pass them — keep the test device-agnostic.
- `power_cycle` drives by **detection, not timers**: it awaits the DUT's USB node
  disappearing then re-enumerating. A `WARNING: …did not re-enumerate` in the log
  means the rail/board is the suspect, not your test.
- `power_cycle` accepts **`reset_via: "esptool"`** — a soft reset that does
  **not** unmap the solenoid channel. Use it when a latched state must survive
  the reset (e.g. a held TCA9548A I2C mux channel — see hil-i2c-strands); a
  true solenoid cold-boot would drop the latch.

### RP2040 / Pico (BOOTSEL + power sequencing)

No esptool 1200-touch here. Each Pico-class device carries a per-device
**`bootsel_channel`** (an MCP23017 solenoid channel physically pressing BOOTSEL;
bank B mirrors bank A power channels, default = power channel + 8) and
**`bootsel_inverted`** (polarity override — some attachments press on OFF).
Bootloader entry = hold BOOTSEL, power-cycle the bank-A channel, release —
the board re-enumerates as the `RPI-RP2` UF2 drive. The wiring lives in the
device record; the stages stay the same shape (`enter_bootloader` → flash →
`power_cycle`), the controller picks the mechanism from the device's `flasher`.

### SAMD51 / UF2 boards (PyPortal, Titano, …)

A default UF2-MSC cycle exists (`SAMD51_FLASH_STAGES` in
`adapters/bench_stages.py`): 1200-baud double-tap into the UF2 bootloader →
**erase the app region** (`bossac --erase` over SAM-BA CDC) → copy the `.uf2`
onto the bootloader MSC drive → `power_cycle`. The erase is NOT optional: a
copy that silently no-ops used to leave stale firmware reporting a false PASS.
A `bossac`-only variant (`SAMD51_BOSSAC_FLASH_STAGES`, Adafruit fork — Debian's
bossac has a broken SAMD51 write applet) flashes a `.bin` at `0x4000`.

## Driving the app under test

After the flash cycle, drive the running firmware with injection stages
(full table in hil-job-api):

- **`inject_protobuf`** — the generic app-poke: publish any BrokerToDevice
  message via the broker's `/api/echo`. If your app talks the broker protocol,
  this drives it without writing a new stage.
- **`inject_i2c_settings`** — Add-with-settings injector for I2C components:
  per-test `{label, name, address, mux_channel, types, settings}` entries, each
  (re)`Add`ed (Add replaces, so changing a setting is just another Add) and
  observed for readings AND rejected-setting errors. Logs
  `I2C_SETTINGS_VERDICT label=… status=ok|error|no_event readings={…} errors=[…]`
  per test. Needs protomq up + secrets pointing at it.
- **`inject_i2c_probe` / `inject_i2c_scan_v1`** — I2C presence checks.
- **Strand auto-inject** — a job whose `target.requires` includes
  `kind: i2c_strand` (matched by component capabilities or model short-names,
  case-insensitive) gets a **`select_i2c_strand` stage auto-prepended**, routing
  that strand's shared I2C bus to the matched DUT before your pipeline runs.
  See hil-i2c-strands. Pair with `power_cycle` `reset_via: esptool` if a reset
  mid-test must not drop the routed strand.

## Verdict contract lines

Stages emit grep-able lines; assert on these, not on prose:

| stage | verdict line | pass condition (typical) |
|---|---|---|
| `verify_checkin` | `CHECKIN_VERDICT ok=true\|false uid=…` | `ok=true` |
| `inject_i2c_settings` | `I2C_SETTINGS_VERDICT label=… status=…` | `status=ok` (or `error` when the setting is *meant* to be rejected) |
| `inject_pixelwrite` | `PIXELWRITE_VERDICT rebooted=true\|false …` | depends on build (A/B compares the two) |
| `capture_display` | `DISPLAY_CAPTURE_VERDICT saved=… exposure_us=…` | `saved=` a real path |

`verify_checkin` is a **WipperSnapper-shaped stage** (it watches the WS broker
check-in topics, v1 and v2 aware) — for a different app, assert on your own
serial/broker lines or add a stage (example C below).

`inject_pixelwrite` reboot detection races a **serial reset-banner** watcher
against **MQTT re-checkin** and takes whichever proves a reboot first — a crash
shows in serial within ~1–2s, long before the device can reconnect to re-checkin.
A WipperSnapper WiFi *re-scan* in the serial log is itself a reboot signal.

## Worked example: WipperSnapper check-in (one app's flow)

Everything below the flash cycle is WipperSnapper-specific: WS reads a
`secrets.json` from its USB-MSC FAT volume, joins WiFi, and checks in to the
broker. The generic lesson is the *sequencing*; the stages are the WS example.

```
enter_bootloader → erase → flash@0x0        # before/after: no_reset throughout
power_cycle                  # boot the app so the USB-MSC volume enumerates
write_secrets_msc            # drop secrets.json (msc_filter auto-derived)
power_cycle                  # reboot WITH secrets → connect WiFi + broker
verify_checkin | inject_…    # assert: it checked in / behaved as expected
```

- **A `power_cycle` MUST precede `write_secrets_msc`.** The MSC FAT volume only
  enumerates after the app boots; flashing with `after: no_reset` leaves the chip
  in the ROM stub, so without the cold-boot the volume never appears and secrets
  can't be written.
- **A second `power_cycle` after secrets** boots the app *with* the broker config
  so it checks in.

The full smoke-test job (the default lightweight gate — proves flash → secrets →
WiFi → broker without injecting anything; cheap, short window):

```jsonc
{ "target": { "device": { "id": "<device>" }, "pool": "public" },
  "script": "firmware-bench",
  "params": { "firmware": { "path": "<uploaded path>", "offset": "0x0" },
    "window_minutes": 3,
    "stages": [ {"type":"enter_bootloader"}, {"type":"erase","before":"no_reset","after":"no_reset"},
      {"type":"flash","offset":"0x0","before":"no_reset","after":"no_reset"},
      {"type":"power_cycle"}, {"type":"write_secrets_msc"}, {"type":"power_cycle"},
      {"type":"verify_checkin"} ] },
  "secrets": { "IO_USERNAME":"…","IO_KEY":"…","WIFI_SSID":"…","WIFI_PASSWORD":"…" } }
```
Assert: `CHECKIN_VERDICT ok=true`.

### Variant B: signal regression (crash vs graceful)

Swap the final stage to `inject_pixelwrite` (pin/color opts). Assert on
`PIXELWRITE_VERDICT`. To compare a release vs a PR build, use the
**hil-firmware-compare** skill (it owns the low/high contract + the A/B summary).

### Variant C: a new signal / behaviour

Add a new stage to `STAGE_HANDLERS` in `adapters/bench_stages.py` (extend the
registry — don't edit the orchestrator), give it a `XXX_VERDICT` line, and assert
on it. New encoders go in `adapters/ws_signal_inject.py`. Check `inject_protobuf`
first — a one-off broker message often doesn't need a new stage.

## Visual verification (camera / ROI)

When a DUT drives a display, assert on what's on screen, not just serial. The
controller crops the DUT's display region from its bench camera:

```bash
# Sharp crop of the device's display, scaled from the calibrated ROI frame:
curl -H "Authorization: Bearer $TOK" \
  "$BASE/v1/devices/$DEVICE/camera/snapshot?res=full&pad=0.05" -o shot.jpg
```

- The ROI is **frame-relative** (`roi_frame_*`), so `res=full` returns a crisp
  sensor-native crop regardless of which frame it was drawn on; `res=warm`
  (default) is the fast low-res path. See [api.md](../../docs/api.md#cameras--rois).
- An eInk/TFT full refresh is slow (~15–20 s) and `res=full` reconfigures the
  camera to a 16 MP still — **don't poll it in a tight loop** (it wedges weak Pis
  like a Zero 2 W). Capture one-shot, ideally driven by a serial "demo done" line,
  and validate *after* the refresh settles rather than racing the live stream.

### Bright self-lit panels (TFT) — use the `capture_display` stage

The `snapshot?res=full` crop above runs the camera on **auto-exposure**, which a
bright self-lit TFT on an otherwise dark bench crushes to near-black (the panel
is a small bright region in a mostly-dark frame). For a readable proof, add a
**`capture_display` stage** to the job instead — it pins a manual exposure, locks
the autofocus-converged dioptre (continuous AF drifts mid-grab and blurs text),
crops the device ROI, and white-patch white-balances off the lit text (the
camera-server ignores `awb`/`colour_gains`, so WB is post-only):

```jsonc
{ "type": "capture_display", "camera_url": "http://rpi-hil006:8080/",
  "exposure_us": 32000, "gain": 3.0,          // 6000/1.0 is near-black on a dark bench
  "roi": [1270, 770, 235, 135, 2304, 1296],   // x,y,w,h,frame_w,frame_h — scaled to the 4608 full frame
  "out": "/tmp/hil-display-capture.jpg" }      // pair with params.collect_artifacts
```

Emits `DISPLAY_CAPTURE_VERDICT saved=… exposure_us=… focus=… wb=yes`. Defaults:
exposure 32000 µs, gain 3.0, `autofocus`+`focus_lock` on, `white_balance` on.
Gotcha: `?full=1` returns the sensor-native 4608×2592 frame while ROIs are
calibrated against the warm 2304×1296 frame — the stage scales by
`roi_frame_*`, so always include the frame size in the ROI.

## Wiring a test into the CI test array

`hil-test-suite.yml` runs an **array of tests**, each its own driver script,
each reported individually in one sticky PR comment:

- The workflow writes the comment marker (`<!-- hil-test-suite -->`) + the `##`
  header **once** in an `Init PR summary` step. Each driver **appends its own
  `### <name>` section** (table) to `hil-out/comment.md` — it must NOT write the
  marker or truncate the file.
- Each driver writes per-test assets to `hil-out/` prefixed by test + side
  (e.g. `${target}-checkin-serial.log`, `${target}-low-flash.log`); they all go
  up in the single `hil-assets` artifact.
- Test steps run with `if: always()` so one failing test still lets the others
  run and report; the job still fails if any test's script `exit`s non-zero.
- Name a regression test after its issue (e.g. "pixelWrite regression (#926)").
- **Poll on a wall-clock deadline, not a fixed iteration count** — firmware-bench
  floods `serial` events so each `/wait` returns instantly; a fixed loop exhausts
  before the ~6-8min pipeline finishes. Break as soon as the `*_VERDICT` line
  appears. Keep `window_minutes` small (≈1) so a finished job frees the device
  for the next test instead of holding it.

Add a test = add a driver script + a `HIL test: <name>` step. The check-in
smoke test (`hil-checkin-run.sh`) and the pixelWrite A/B (`hil-pixelwrite-run.sh`)
are the worked examples.

## Timeouts

- `timeouts.total_s` — hard ceiling for non-interactive scripts; **not** applied
  to `firmware-bench` (its lease/`window_minutes` owns the deadline).
- `params.window_minutes` — how long the device is held after the pipeline. Keep
  it small (≈3) for a quick test; larger only if a human will interact.
- A generous *job* deadline (≈30 min) is deliberate so a slow reboot/checkin
  isn't missed; tighten to ≈5 min for smoke tests that don't build firmware.
- Per-stage: `checkin_timeout_s` (120), `observe_s` (30, reboot watch),
  `disappear_timeout_s`/`reappear_timeout_s` (10/30, power_cycle),
  `observe_s` (12) per `inject_i2c_settings` test.

## Rules

- **Never put secret values in a committed file.** Pass `IO_*` / `WIFI_*` from the
  environment at submit time.
- **Never simulate hardware.** A target that won't run is skipped + reported via
  `GET /v1/targets`, never faked.
- **Never filter USB by VID/PID** (a DUT changes VID across modes) — match by
  by-id/by-path/iSerial/label.
- Don't add `launch_protomq`/`start_serial_log`/`print_boot_log` manually unless
  you need explicit placement; they're auto-inserted.
- Assert on the **verdict lines**, and on the **finished** captured logs where you
  can, not the racing live stream.
