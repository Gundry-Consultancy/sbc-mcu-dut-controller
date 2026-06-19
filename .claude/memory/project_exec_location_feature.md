---
name: project-exec-location-feature
description: IMPLEMENTED (branch feat/per-phase-exec-location) — per-phase execution-location (build/flash/test/protomq on controller vs DUT host) for arduino-ws jobs
metadata:
  type: project
---

**STATUS (2026-05-27): merged to `main`. usbip bridge PROVEN on hardware
(`usbip-attached 1-1.1.1.4 → /dev/ttyACM0` in job f59e7f91's deploy.log).
Build-on-controller (aarch64) WORKS for ESP32-S3 once the PIO cache is clean.**
- **TOOLCHAIN "Dynconfig for esp32s3 not exist" was a STALE/CORRUPT PIO cache,
  NOT an aarch64 gap (corrected by tyeth 2026-05-27).** Fix that worked: ssh as
  particle, `source ~/.platformio/penv/bin/activate`, `rm -rf
  ~/.platformio/packages ~/.platformio/platforms`, then `pio run -e
  adafruit_feather_esp32s3_reversetft --verbose` from /tmp/hil/<id>/ — clean
  re-download installs the proper `…/toolchain-xtensa-esp-elf/lib/
  xtensa_esp32s3.so` and the build succeeds on Tachyon. `~/.platformio` is the
  shared PIO core home (packages/platforms) used by every job's `.venv`, so
  wiping it fixes builds for all jobs. If "Dynconfig not exist" recurs, wipe the
  cache rather than blaming the arch.
- **Deploy/build logs are now findable in the UI** (commit 92f5662): the worker
  writes deploy stdout+stderr to `{jobs_dir}/{job_id}/deploy.log` as a
  `kind='log'` asset (success AND failure), linked from the job-detail page,
  served at `GET /ui/assets/{id}/view`. PATs (`https://<tok>@`, `ghp_`,
  `github_pat_`) are redacted in captured logs + the deploy:info event. (User
  chose redact-not-rotate; 3 pre-fix event rows (7224d73e/b30cf35f/d6a67ac9
  seq3) still hold the live PAT — user chose to leave them as-is, not scrub.)
- **FOLLOW-UP (not done):** `deploy.log` assets are inserted with `purge_at
  NULL`, so they accumulate under `run/jobs/{job_id}/` forever (unlike firmware
  assets which carry a purge date). Given prior disk-space pain on the fleet,
  give log assets a default retention / include them in purge-eligible sweeps.

**STATUS (2026-05-27): merged to `main`, hardware run in progress.**
- **LEASE-DEADLOCK FIX (commit a253ff9):** the scheduler
  (`queue/scheduler.py`) already holds an `exclusive_device` lease for the
  **entire** job (deploy+flash+run, released in its `finally`). The original
  `_flash_usbip` *also* acquired an `exclusive_device` lease for the same
  device+job → it conflicted with the job's own lease (`device … blocked by
  lease #N`) and the flash phase errored on every run. Fix: phase adapters
  must NOT acquire device leases — the scheduler owns job-scoped exclusivity.
  Dropped the adapter's lease + `db_path` param; the usbip bridge CM already
  self-tears-down. **Invariant for any future phase adapter: rely on the
  scheduler's lease, never re-acquire.** See [[reference-bench-host]] for the
  stale-vs-race lease triage (`released_at IS NULL` = truly held).

(historical) implemented on branch `feat/per-phase-exec-location`,
5 commits (PR1–PR5), full test suite green; not yet hardware-validated.
- PR1 `adapters/usbip_bridge.py` (UsbipBridge.attached() CM + parsers)
- PR2 `adapters/arduino_ws_exec.py` (phase router, lease-wrapped usbip flash,
  ship-artifacts fallback) + `hosts/registry.py` make_adapter routing
- PR3 builder emits `params.exec`; `_default_build_steps` now compile-only
  (no `--target upload`); `config.controller_ip` (HIL_CONTROLLER_IP, .169)
- PR4 `deploy/topology.example.yaml` controller host + usbip-wired revtft
- PR5 `setup-hil-host.sh` sudoers/modules; AGENT_HANDOFF M7 section
- **OPEN QUESTION RESOLVED** (tyeth): flash via usbip-attach to controller
  (model A). ship-artifacts is implemented as the fallback.
- **NOT DONE:** live-hardware re-enum validation. Bridge is one-shot (no
  vendor/usbip-autoattach reconciler) so ESP32-S3 flash re-enum may drop the
  attach — validate cheaply (bind/attach/esptool chip-id+read-mac) before
  trusting usbip; else switch job to `flash_mode=ship-artifacts`. See
  AGENT_HANDOFF "Per-phase execution-location (M7)".


Planned arduino-ws feature (direction set 2026-05-27 by tyeth): make each job
phase's **execution host** selectable instead of hardcoded to the DUT host.

Axes: **build** (`pio run`), **flash**, **test/pytest**, **protomq** — each
`controller` | `dut-host` (protomq also `off`).

**Why:** rpi-displays (the DUT host) is too weak to compile WipperSnapper
(415 MB RAM, swaps/OOMs — see [[reference-rpi-displays-compute]]). The controller
host Tachyon (192.168.1.169, 8-core/multi-GB) can. So compiles should route to
the controller.

**Near-term target (tyeth, 2026-05-27):** build on .169, protomq on .169,
pytest = none, DUT options otherwise unchanged (device, flash target, protomq
play-script e.g. `reverse-tft-s3-demo`). Under this layout the DUT's `MQTT_HOST`
= Tachyon LAN IP (192.168.1.169), not 127.0.0.1.

**How to apply / implementation notes:**
- `hil-controller` runs ON Tachyon, so "build on controller" = `LocalTransport`
  (registry.py already builds it for `host.kind=="local"`). "flash on DUT host" =
  the existing `SSHTransport`.
- Core change: `GitDeployAdapter` is single-transport today (`self.transport`
  threads clone/setup/run, git_deploy.py). It must become **multi-transport**
  with per-phase routing.
- **Gotcha:** flashing on the DUT host from a controller build can't ship only
  `firmware.bin` — esptool needs `bootloader.bin` + `partitions.bin` +
  `boot_app0.bin` + `firmware.bin` at offsets, so transfer the whole
  `.pio/build/<env>/` set. (Model A — usbip-export the DUT to the controller and
  flash there — avoids transfer but hits ESP32-S3 re-enum fragility over usbip.)
- Relationship to [[reference-rpi-displays-power]] usbip leasing: leasing decides
  *who owns the USB port*; this decides *where each phase runs*. Complementary.

**OPEN QUESTION (unresolved):** where does flash happen when build is on .169 —
usbip-attach the DUT to the controller and flash there, or ship artifacts to the
DUT host and flash locally?
