---
name: project_firmware_bench
description: firmware-bench interactive flash + protomq hold sessions — design, modules, constraints
metadata:
  type: project
---

`script: "firmware-bench"` is an interactive, time-boxed (default 30 min,
extendable) hold on a DUT — flash a combined `.bin`, optionally write
`secrets.json` to the MSC drive pointing at a freshly-launched protomq,
power-cycle, then keep serial + protomq logging alive for the window. Drivable
manual (web), API (LLM), or pytest (protomq HTTP API autoresponders).

Modules: `adapters/flashers/esptool.py` (added `--after no_reset`, `verify()`,
`soft_reset()`, `bootloader_touch_1200()`); `adapters/bench_stages.py`
(composable ordered stage list, each → existing adapter; `flash` picks
esptool/pio; registry `STAGE_HANDLERS`); `adapters/msc_secrets.py` (resolve
by-id filter → udisksctl mount → tee secrets.json → unmount);
`adapters/protomq_launcher.py` (clone+build+`npm start`, parse reported ports);
`adapters/firmware_bench.py` (orchestrator + hold loop). Integration:
`leases.renew()`/`get_active_for_job()`, worker `bind_runtime` hook + skips
`total_s` for interactive scripts, scheduler sets initial lease `expires_at`,
`POST /v1/jobs/{id}/extend`, registry dispatch, web form `job_new_firmware_bench.html`.

Hard constraints from the user (do not violate):
- **NEVER edit `vendor/protomq`** — launcher parses the port lines it already
  prints; protomq is cloned+built fresh per session.
- Split hosts: flash/serial/MSC on the DUT host; protomq on the controller
  (DUT reaches it at `controller_ip`). See [[project_exec_location_feature]].
- Two serial ports: flash-mode port ≠ post-reboot logging port.
- MSC/port locators are **filters** (job overrides DUT-profile filter), matched
  on iSerial/by-id/label, never VID — see [[feedback_never_filter_usb_by_vid]].
- No solenoid_channel → power_cycle falls back to esptool soft reset (warn).
  Solenoid per-port power map is in [[reference_hil_bench_usb_topology]].

Gotcha: literal `/jobs/<name>` GET routes in `web/router.py` MUST be defined
*before* the `/jobs/{job_id}` catch-all or they 404 (this bit new-arduino too).

**Live-validated 2026-06-13** against `mcu-qtpy-oled-091-stemma` (QT Py ESP32-S3,
ch4 @ `1-1.2`, by-path `/dev/serial/by-path/platform-3f980000.usb-usb-0:1.2:1.0`):
full erase→flash(3.1MB)→verify→solenoid-power-cycle→live-serial-hold→extend all
worked via the web UI + API. Fixes landed during the run:
- **Registry matches devices from `run/topology.yaml` in-memory, NOT the DB** —
  so `serial_port` (and any field an adapter reads off the device) must be in
  topology.yaml; setting it only in the DB is invisible to matching. Seeder now
  also preserves a DB-set `serial_port` across re-seed.
- **socat** splits `OPEN:` addresses on `:`/`,` → by-path names silently
  captured nothing until escaped (`serial_capture._socat_escape`).
- **SerialCaptureAdapter reconnects** now: a single open dies if the CDC isn't
  enumerated yet right after a power-cycle; it retries every `reconnect_s`.
- Web stage checkboxes default to `""` (unchecked-absent), not `"on"`.
- **esptool flakiness:** ESP32-S3 USB-Serial/JTAG flash is intermittent from a
  running app ("Could not configure port: Input/output error" on open) but
  reliable once in the download/bootloader ROM. Re-running usually catches it;
  a retry on transient serial errors in EsptoolFlasher is the real fix (TODO).

**protomq launch recipe (2026-06-13, proven to start on tachyon):**
- protomq ref = **displays-v2-testing**; protobuf source = **adafruit/Wippersnapper_Protobuf @ api-v2**
  cloned as the SIBLING `../Wippersnapper_Protobuf` (`.env.example.json` →
  `protobufSource: ../Wippersnapper_Protobuf/proto`). V1 protos are bundled in
  protomq's branch (the "No protobufSourceV1, skipping V1" message is fine).
- Build: `cp -f .env.example.json .env.json && npm ci && npm run import-protos &&
  npm run build-web`, then `npm start` → binds MQTT 1884 / API 5173 / WS 8888.
- Without api-v2 protos the broker dies on start: `Error: no such type:
  signal.BrokerToDevice`. The `main` branch lacks `.env.example.json` entirely.
- tachyon has node v22 / npm 10. Clones auth via PAT-in-URL (preferred) or
  `HIL_GIT_CREDENTIAL_HELPER='!sudo gh auth git-credential'` (set in controller.env).
- Logs: serial.log / protomq.log / **flash.log** (full esptool command transcript
  with chip id + MAC + verify) are registered as downloadable 'log' assets at
  teardown; the Assets page now has View/Download links.

**FLASH-FROM-APP — SOLVED & PROVEN END-TO-END (2026-06-13).** The earlier
"BLOCKER" note was WRONG. A QT Py ESP32-S3 running the WipperSnapper TinyUSB app
*does* enter download mode via a `stty -F <by-path> 1200` touch on the CDC port.
The prior failures were two avoidable mistakes, not a hardware limit:
1. **by-id is NOT stable across the mode flip; by-path IS.** App mode enumerates
   as `usb-Adafruit_QT_Py_ESP32-S3…-if00`; after the 1200-touch the device
   re-enumerates as the ROM bootloader `usb-Espressif_USB_JTAG_serial_debug_unit_<MAC>-if00`
   — a *completely different by-id*. The **by-path** (`/dev/serial/by-path/platform-3f980000.usb-usb-0:1.2:1.0`)
   stays valid across the flip. Always drive esptool by **by-path**.
2. **In app mode `--before default_reset` fails** ("Could not configure port:
   I/O error") — the TinyUSB CDC ignores DTR/RTS. The touch is what flips it.
   Once in the ROM, every esptool step must use **`--before no_reset`** or the
   USB-Serial/JTAG drops back out of download mode.

Proven manual cycle (by-path, esptool v5.2.0): `stty -F <by-path> 1200` → sleep 3
→ `python3 -m esptool --chip esp32s3 --port <by-path> --before no_reset --after
no_reset {erase_flash | write_flash 0x0 combined.bin | verify_flash 0x0 combined.bin}`
→ `~/turn_off.sh 4; sleep 2; ~/turn_on.sh 4` → app boots, **WIPPER MSC drive
appears** as `sda` at by-path `…usb-0:1.2:1.2-scsi-0:0:0:0` (label WIPPER).
`enter_bootloader` stage now does touch-FIRST (no upfront hub reboot — that just
boots the app back); hub power-cycle is the recovery only if the touch loop fails.

**MSC secrets — udisksctl FAILS for the `pi` user (polkit: NotAuthorized, no
login session over SSH). Use the sudo-mount fallback** (implemented in
`write_secrets_to_msc`): `sudo mount -t vfat -o rw,uid=$(id -u),gid=$(id -g)
<realdev> /tmp/hil-msc-<dev>` → `tee secrets.json` → `sync` → `sudo umount`. `pi`
has passwordless sudo. The WipperSnapper app pre-populates the FAT with
`secrets.json` + `wipper_boot_out.txt`. msc_filter is by-path `usb-0:1.2:`
(matches the `:1.2-scsi` MSC interface under /dev/disk/by-path).

**protomq teardown leak (fixed 2026-06-13):** `npm start` → `sh -c node main.js`
→ `node main.js`; `proc.terminate()` killed only npm, orphaning node on 1884 for
an hour after a cancelled job. Fix: spawn with `start_new_session=True` and
`os.killpg(getpgid(pid), SIG…)` the whole group on stop.

**protomq launch timing:** now a `launch_protomq` STAGE injected after erase /
just before flash (was launched upfront) — never orphans a broker when erase
fails. **Serial bootlog:** a `start_serial_log` stage is injected before the
first `power_cycle` so the reboot's early boot lines are captured. **All stages**
(solenoid, stty, udisksctl/mount, tee) now record their CLI command + output to
flash.log via a `_RecordingTransport` wrapping dut/hub transports.

**Combined.bins for testing (in `~/Downloads/hil-bins/` on dev box):**
`wippersnapper.qtpy_esp32s3_n4r2.fatfs.1.0.0-adafruit-d2fbe0dd.combined.bin`
(PR build) and `…1.0.0-beta.129.combined.bin` (release) — both 3150160 bytes,
flash at 0x0, expose the WIPPER MSC drive a few seconds after boot.

**Live verification (2026-06-13, commits e0b2ada→9fe7f03 on the tachyon):**
- PROVEN live in a real UI-submitted job: touch-first entry (`app mode → stty
  1200 → download mode reachable after 1 touch`), protomq launching AFTER erase
  (stage 3), serial starting BEFORE the reboot, power-cycle CLI in the transcript
  (`$ python3 /opt/hil/solenoid_hub_cli.py port_off 4 → exit 0`), protomq killpg
  teardown (no orphan node after cancel/error), and the `/ui/jobs/new-arduino`
  404 fix (200 now). MSC wait-for-enumerate + print_boot_log are unit-tested but
  not yet seen end-to-end live (flash kept failing first — see below).
- **ESP32-S3 JTAG flash flakiness is REAL and intermittent.** Same bin: my manual
  flash + job bc8a7998 flashed one-shot at 921600; job 99cea56d (freshly touched)
  hit `A serial exception error occurred: Write timeout` — the stub uploads/runs
  but the bulk `write_flash` wedges instantly, and re-running esptool on the same
  dead endpoint just times out again. Added `_recover_download_via_hub`: on a
  flash FlasherError the flash stage power-cycles (fresh USB endpoint) → re-enters
  download mode → retries (recover_attempts, default 2). Shared with
  enter_bootloader's touch-loop-failed escalation.
- **`is_in_download_mode(timeout=)` over SSH does NOT kill the remote esptool** —
  `asyncio.wait_for` cancels the Python await but the esptool process keeps
  holding the port on rpi-displays, so the next probe/touch blocks on a busy
  port and the touch loop hangs. OPEN: kill the stale remote esptool on timeout
  (e.g. `fuser -k <port>` / pkill by port) before retrying. Quick win.
- **The QT Py self-reboots ~every 30s with no/invalid secrets (faster if it
  crashes).** In APP mode the serial node hops ttyACM0↔ttyACM4↔gone and every op
  races the reboot; in DOWNLOAD mode (ROM, no app) it's stable, which is why
  flashing must stay `--before no_reset` in the ROM. After many failed flashes
  the device gets into a degraded self-rebooting + JTAG-wedged state where even
  `flash_id` times out; a single hub power-cycle didn't always clear it.
- **Solenoid ch4 DOES cut the QT Py's power** (confirmed: stayed absent from lsusb
  45s after `turn_off 4`, past the ~30s self-reboot) — but the self-reboot churn
  makes the lsusb-disappears check racy, and a Pi reboot does NOT reset the
  independently-powered MCP23017 latch. ch4↔port 1-1.2↔QT Py mapping is correct
  ([[reference_hil_bench_usb_topology]]). For a wedged/degraded device the most
  reliable recovery is a physical BOOT-button hold during power-on (forces ROM,
  bypasses the app + touch race) — needs bench access.

**"Erase log shows no erasing" — ROOT CAUSE (2026-06-14):** it is NOT a logging
bug. `EsptoolFlasher.erase()` records full stdout/stderr via `_RecordingTransport`
(a success WOULD show `Flash memory erased successfully in Ns`). The log shows no
erasing because esptool never gets past `Connecting......` → `A fatal error
occurred: Failed to connect to ESP32-S3: No serial data received.` (EXIT 2). The
erase phase never runs, so there is nothing to log. Any esptool step (flash_id /
erase / write / verify) on a wedged endpoint dies at the connect phase identically.
**Remote recovery can be insufficient.** Today the QT Py (ch4) was wedged in this
exact state and I exhausted every remote lever WITHOUT clearing it:
- genuine device power-cycle — `turn_off.sh 4` latched off (confirmed: 1-1.2 was
  ABSENT after a Pi reboot reset the host controller), then `turn_on.sh 4` →
  device re-enumerated FRESH (new by-id mtime) still as `Espressif USB JTAG…` →
  esptool STILL "No serial data received".
- Pi host USB-controller reboot — no change.
- `esptool --before usb-reset` (native-USB re-sync) — failed (EXIT 1).
- `sudo systemctl stop ModemManager` then probe — ruled OUT the MM-steals-the-port
  theory; still "No serial data received". (MM is active on rpi-displays; it does
  grab new ttyACM transiently, but it is NOT the cause of this wedge.)
So a USB-Serial/JTAG that enumerates but returns no sync survives a clean
power-cycle → it needs the **physical BOOT(GPIO0)-held + RESET** to force genuine
serial download mode. The earlier note "power-cycle for a fresh endpoint clears
the JTAG wedge" is only SOMETIMES true; this deeper wedge needs bench access.
**Gotcha that misled me first:** a wedged device LINGERS in `/sys/.../1-1.2` after
its power is latched off — it only shows ABSENT once the USB host controller
resets (Pi reboot). So "device still present after all_off" does NOT mean ch4
power-switching failed; ch4 works (proven by the post-reboot ABSENT). Don't
re-diagnose the solenoid over this.
**HIL pixelWrite regression pipeline (2026-06-14) — built end-to-end.** Proves
#927's fix: a v1 `signal.v1.PixelsRequest{ req_pixels_write: pin "D0", colour
200 }` (11-byte payload `1a 09 08 01 12 02 44 30 18 c8 01`) to an uninitialised
strand CRASHES release `1.0.0-beta.127` (null-deref: pin 0 ↔ zero-init sentinel)
but the fix logs `ERROR: Pixel strand not found` + continues. Pieces (all
committed/deployed on tachyon):
- `adapters/ws_signal_inject.py` + `inject_pixelwrite` stage: fires the pixelWrite
  via protomq `POST /api/echo` to `<user>/wprsnpr/<uid>/signals/broker/pixel`
  (protomq has V1 autoresponders on `+/wprsnpr/#`; `/api/echo`, `/api/autoresponse`,
  `/api/scripts/:n/steps/:s/send` are the inject endpoints; do NOT edit
  vendor/protomq). Detects crash by a fresh MQTT re-checkin → logs
  `PIXELWRITE_VERDICT rebooted=true|false`.
- `GET /v1/jobs/{id}/assets` + `/assets/{id}/download` (api/jobs.py): CI pulls
  serial/protomq/flash logs as proof.
- Device availability: `GET /v1/targets`, DB columns + idempotent migration,
  `availability.py` policy (temp self-heals ≤3×/~3min, perm never) +
  `availability_reconciler.py`; `build_target` tag (arduino-cli name) is what
  /v1/targets keys off. **The seeder's ON CONFLICT does NOT touch
  status/build_target/availability → DB-set values survive re-seed (durable);
  model/capabilities/port ARE re-seeded from topology.yaml.** See
  [[reference_device_availability]] / docs/device-availability.md.
- Firmware delivery: `params.firmware.url` (controller downloads, sha256) +
  `POST /v1/firmware` (raw-body upload → controller-local path); firmware-bench
  copies that local path to the bench.
- `.claude/skills/hil-firmware-compare` (agnostic A/B runner). CI:
  `adafruit/Adafruit_Wippersnapper_Arduino` PR #930 branch `hil-test-additions`
  → `.github/workflows/hil-test-suite.yml` + `hil-pixelwrite-run.sh` (workflow_run
  after "WipperSnapper Build CI" → Tailscale → /v1/targets → fetch release zip +
  PR artifact → upload → 2 firmware-bench jobs → assert verdict → PR comment).
  Needs repo secrets TAILSCALE_AUTHKEY_TYETH + HIL_API_TOKEN; pending first live run.
- **Inventory fix:** the live QT Py S3 (ch4, 1-1.2) is DB record
  `mcu-feather-eink-29-rbw` (was mislabelled "Feather ESP32 V2") → build_target
  `qtpy_esp32s3_n4r2`, available. `mcu-qtpy-oled-091-stemma` is actually a QT Py
  **S2** duplicate → relabelled `qtpy_esp32s2`, offlined. All other MCU DUTs
  offlined (only qtpy enrolled). Model relabel in topology.yaml is a cosmetic
  follow-up (build_target tag already drives CI).

**dwc2 vs dwc_otg (2026-06-14, tested live on rpi-displays / Pi Zero 2 W):**
- The stock RPi kernel (`6.18 rpi-v8`) has **dwc_otg built-in**; the
  `dtoverlay=dwc2,dr_mode=host` in the default config is scoped to **`[cm5]`**
  so it's INERT on a Zero 2 W. To actually switch you must add the overlay under
  **`[all]`** (then reboot) — `setup-hil-host.sh`'s "already present" grep must
  not false-match the `[cm5]` line (known gap). Restore + reboot reverts.
- **dwc2 makes a *failing* device cycle FAR faster** (measured ~506 USB
  connect/disconnect events / 5 s vs ~1 / 5 s under dwc_otg) — and esptool then
  syncs but the chip "stopped responding" mid-op, so a multi-second flash can't
  complete. dwc_otg lets the same board settle to the slow ~2.2 s cycle where
  default_reset can catch AND hold it. **For flashing a misbehaving native-USB
  board, dwc_otg is better; dwc2's only win is not wedging the whole bus.**
- **The QT Py (ch4) is a HEALTHY board with BLANK flash** (`invalid header:
  0xffffffff`, TG0WDT ~2s loop) — restorable by flashing, NOT faulty.
- **E2E FLASHED SUCCESSFULLY 2026-06-14** (commit f5a1ae5 deployed): caught on
  attempt 1 via `--before default_reset` → **`Flash memory erased successfully
  in 17.0s`** (this is the answer to the long-standing "erase log shows no
  erasing": the erase only logs once the chip is actually IN download, which the
  default_reset entry guarantees) → wrote 3 150 160 B (Hash verified) → **Verify
  successful (digest matched)** → `invalid header` count dropped 100%→0 (loop
  gone). All esptool steps `--before no_reset --after no_reset`; entry only via
  default_reset.
- **CATCH RELIABILITY depends on a FRESH state**: `default_reset` catches first
  try right after a Pi reboot / fresh power-on, but DEGRADES after repeated
  reset churn (every attempt then hangs to the timeout or hits `OSError 71
  EPROTO` on setRTS). So the bench's recovery should power-cycle (fresh USB
  endpoint) THEN catch — which is exactly what `_recover_download_via_hub` does
  (power_cycle → force_download_via_reset). Use `timeout -k` to force-kill a hung
  esptool. Single `--connect-attempts 1` in a tight loop beats `--connect-attempts
  N` (one bad reset can wedge the whole invocation).
- **BOTH bins boot NORMALLY (2026-06-14) — app serial confirmed.** After flash +
  a real reset the QT Py enumerates as `239a:8143` (Adafruit QT Py TinyUSB) and
  runs WipperSnapper; boot log shows `Loaded app from partition at offset
  0x10000` then the EXPECTED no-secrets error (PR build: "Invalid IO credentials
  in secrets.json"; beta.129: "Please edit the secrets.json file"). The PR build
  is NOT broken — earlier "silent app" was reading while it sat in download/ROM,
  not after a clean app boot. Both flash ENTRY ROUTES validated live: blank/
  boot-loop → `default_reset`; running app → **1200-touch (caught on touch #1)**,
  which is why default_reset *fails* once the app is running (use the touch).
- **Reset-into-app over USB-Serial/JTAG is unreliable**: `--after hard_reset`
  ("via RTS pin") and `--after watchdog_reset` did NOT reboot the QT Py into the
  app (stayed silent in download). Boot the flashed app via a real power-cycle
  (solenoid) — which is the bench's `power_cycle` stage anyway. `/tmp` on
  rpi-displays is tmpfs — scripts there are wiped on reboot; re-deploy after.

**Bench-disturbance lesson:** running `all_off` to test ch4 destabilised the
`1-1.1` Genesys sub-hub (`-71`/`-110` storm, ch0/1/2 downstream gone); only a Pi
reboot cleared it. Prefer per-channel `turn_off.sh <ch>` over `all_off` for
single-DUT power-cycles. See [[reference_hil_bench_usb_topology]].

**REFINED ROOT CAUSE (2026-06-14) — supersedes the "needs physical BOOT" note.**
With a freshly-rebooted (clean) `dwc_otg`, `dmesg -w` showed the QT Py
**re-enumerating every ~2.2s**: connect (idVendor=303a idProduct=1001 "USB
JTAG/serial debug unit", the S3 ROM USB-Serial/JTAG) → `cdc_acm ttyACM0` →
`USB disconnect` 2.2s later → repeat, fresh device number each time. esptool's
multi-second sync can never complete in a 2.2s window → every step dies at
"Connecting... No serial data received". Stopping ModemManager did NOT stop the
cycle (so MM is not the trigger here) — it's either the chip self-resetting on
a ~2s watchdog or `dwc_otg` port-resetting a device it deems stuck.
- **rpi-displays is a Pi Zero 2 W** (BCM2837, same family as Pi 3B), legacy
  `dwc_otg`, Wi-Fi (`brcmfmac`/SDIO) — network is independent of USB.
- **`dwc_otg` host-controller wedge is real and host-side:** under USB error/
  reset storms it hangs with `WARN::dwc_otg_hcd_urb_dequeue: Timed out waiting
  for FSM NP transfer to complete` on every endpoint; bus dies, /dev nodes go
  stale (a device latched OFF can LINGER in sysfs until the HCD resets — don't
  misread that as "power didn't cut"). **Only a reboot clears it.**
- **NEVER unbind/rebind `dwc_otg`** (`3f980000.usb`): the re-probe Oopses
  (NULL deref in `dwc_otg_driver_remove`), deregisters USB bus 1, reboot-only
  recovery. I did this and had to reboot. Use `dwc2` if you want a rebindable
  controller.
- **ModemManager now masked on the bench** (it toggles DTR/RTS = EN/IO0 on
  USB-Serial/JTAG → resets the chip); `setup-hil-host.sh` masks it. README +
  setup-script documented & deployed (commit a8207b8). **esptool policy:
  always `--before no-reset --after no-reset`, retry 3×.**
- **OPEN / next lever:** switch rpi-displays to mainline **`dwc2`**
  (`dtoverlay=dwc2,dr_mode=host` in `/boot/firmware/config.txt` + reboot) — if
  the 2.2s cycle is `dwc_otg` port-resetting, dwc2 may hold the link steady so
  no-reset esptool can sync. setup-hil-host.sh auto-enables dwc2 on Pi Zero
  models, opt-in (`HIL_USE_DWC2=1`) elsewhere. NOT yet applied live.
