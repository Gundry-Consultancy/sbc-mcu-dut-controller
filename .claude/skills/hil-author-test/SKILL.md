---
name: hil-author-test
description: "Author a hardware-in-the-loop (HIL) test script or example against the usbip-hil-controller — flash real firmware on a bench DUT, drive it (secrets, power-cycle, optional signal injection), and assert on captured logs. Use when writing a NEW test/example (a check-in smoke test, a signal regression, a boot/version check, a custom flash sequence) or a CI step that runs one. Covers the firmware-bench stage vocabulary, the job request shape, verdict contract lines, timeouts, and the proven stage ORDER. For the specific two-build A/B regression pattern, defer to the hil-firmware-compare skill. NOT for: a single ad-hoc manual flash (submit a firmware-bench job directly)."
---

# hil-author-test

How to write a HIL test against the controller. A test = **flash a firmware on a
real DUT, bring it to a known state, do something, and assert on what the
hardware reported** (serial / broker / esptool logs). The controller's
`firmware-bench` script runs an ordered **stage pipeline**; you compose stages
and assert on machine-greppable verdict lines.

Read [`docs/api.md`](../../docs/api.md) for the full endpoint + stage reference;
this skill is the *authoring* guide on top of it.

## The shape of every test

1. **Pick a target** — `GET /v1/targets` → run only `available` ones, skip +
   report the rest (temporary → give the controller its ≤3-try/~3-min heal
   budget; permanent → skip now). Never simulate hardware.
2. **Get firmware onto the controller** — `POST /v1/firmware?filename=…` (raw
   body) and use the returned `path`, or set `params.firmware.url` and let the
   controller download it.
3. **Submit a `firmware-bench` job** — `POST /v1/jobs` with a stage pipeline,
   `secrets`, and timeouts (below).
4. **Wait** — long-poll `GET /v1/jobs/{id}/wait?since=&timeout=`; stop on a
   terminal `state` (`finished|failed|cancelled|error|timeout`). Filter out the
   `serial` log stream so it doesn't drown your poll.
5. **Pull proof** — `GET /v1/jobs/{id}/assets` → download `serial.log` /
   `flash.log` / `protomq.log` (all UTC-ms timestamped) + the `boot_out.txt`
   version. Prefer asserting on the **finished** captured logs over racing the
   live stream — unless the DUT-host itself runs the assertion.

## The proven stage order (don't reinvent it)

For an ESP32 WipperSnapper combined.bin, the order that actually works:

```
enter_bootloader            # 1200-touch → USB-JTAG reset → hub recovery
erase                       # before/after: no_reset
flash        @0x0           # before/after: no_reset
power_cycle                 # boot the app so the USB-MSC volume enumerates
write_secrets_msc           # drop secrets.json (msc_filter auto-derived)
power_cycle                 # reboot WITH secrets → connect WiFi + broker
verify_checkin | inject_…   # assert: it checked in / behaved as expected
```

Critical gotchas baked into this order:
- **A `power_cycle` MUST precede `write_secrets_msc`.** The MSC FAT volume only
  enumerates after the app boots; flashing with `after: no_reset` leaves the chip
  in the ROM stub, so without the cold-boot the volume never appears and secrets
  can't be written.
- **A second `power_cycle` after secrets** boots the app *with* the broker config
  so it checks in.
- `launch_protomq` (before first flash), `start_serial_log` (before first
  power_cycle), and `print_boot_log` (after last power_cycle) are **auto-inserted**
  — don't add them unless you need explicit placement.
- The controller **fills bench specifics itself**: the protomq broker host:port
  written into `secrets.json`, and the `msc_filter` (derived from the device's
  by-path serial). Don't pass them — keep the test device-agnostic.
- `power_cycle` drives by **detection, not timers**: it awaits the DUT's USB node
  disappearing then re-enumerating. A `WARNING: …did not re-enumerate` in the log
  means the rail/board is the suspect, not your test.

## Verdict contract lines

Stages emit grep-able lines; assert on these, not on prose:

| stage | verdict line | pass condition (typical) |
|---|---|---|
| `verify_checkin` | `CHECKIN_VERDICT ok=true\|false uid=…` | `ok=true` |
| `inject_pixelwrite` | `PIXELWRITE_VERDICT rebooted=true\|false …` | depends on build (A/B compares the two) |

`inject_pixelwrite` reboot detection races a **serial reset-banner** watcher
against **MQTT re-checkin** and takes whichever proves a reboot first — a crash
shows in serial within ~1–2s, long before the device can reconnect to re-checkin.
A WipperSnapper WiFi *re-scan* in the serial log is itself a reboot signal.

## Canonical examples

### A. Check-in smoke test (the default lightweight gate)

Proves the whole path (flash → secrets → WiFi → broker) without injecting
anything. Cheap; use a short window.

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

### B. Signal regression (crash vs graceful)

Swap the final stage to `inject_pixelwrite` (pin/color opts). Assert on
`PIXELWRITE_VERDICT`. To compare a release vs a PR build, use the
**hil-firmware-compare** skill (it owns the low/high contract + the A/B summary).

### C. A new signal / behaviour

Add a new stage to `STAGE_HANDLERS` in `adapters/bench_stages.py` (extend the
registry — don't edit the orchestrator), give it a `XXX_VERDICT` line, and assert
on it. New encoders go in `adapters/ws_signal_inject.py`.

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
  (default) is the fast low-res path. See [api.md](../../../docs/api.md#cameras--rois).
- An eInk/TFT full refresh is slow (~15–20 s) and `res=full` reconfigures the
  camera to a 16 MP still — **don't poll it in a tight loop** (it wedges weak Pis
  like a Zero 2 W). Capture one-shot, ideally driven by a serial "demo done" line,
  and validate *after* the refresh settles rather than racing the live stream.

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
  `disappear_timeout_s`/`reappear_timeout_s` (10/30, power_cycle).

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
