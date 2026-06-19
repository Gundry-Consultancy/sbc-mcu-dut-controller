---
name: hil-ci-pipeline-state
description: State of the GitHub-CI HIL regression pipeline (PR #930) + where to resume — see docs/HANDOFF.md
metadata:
  type: project
---

**✅ PROOF QUOTES FIXED + clean 3/3 again (run 27586759035, 2026-06-16 ~01:14Z).**
User asks: (1) serial quote should be FROM BOOT by default, (2) the serial & protomq
quoted sections drifted to different minutes (protomq "missed the event", useless for
comparison). ROOT CAUSE of (2): each log was windowed by a fixed LINE count, but the
broker (protomq) log is far chattier than serial, so N lines = wildly different
wall-clock durations → the two windows landed in different time ranges. FIX (WS commit
62dcba02, `hil-lib.sh`): `HIL_PROOF_BEFORE` now defaults to **-1 = from boot**;
`proof_window` records its window's UTC-ms span; new `time_window()` selects protomq
lines within the SAME wall-clock span as the serial quote (logs share one clock; fixed-
width ISO-8601 UTC sorts lexically=chronologically). Serial is the time reference; the
protomq section is aligned to it ("⏱ aligned to the serial window (…)"). ALSO fixed a
`set -u` crash: `proof_window` runs in a command-substitution subshell so its PW_TS_*
globals never reached the caller — referencing them under set -u crashed AFTER the
verdict (check-in showed ok=true but the step FAILED and emitted no proof sections);
now the span is derived from the captured window text. Verified live: all 3 sections
show "✓ from boot…" serial + protomq window contained within the serial span. +5
hil-lib.test.sh checks (since-boot window, recorded span, time_window selection,
append_proof under set -u). See [[msc-mount-udisksctl-noise]].

**✅✅ FULLY-VALIDATED CLEAN RUN on rpi-hil006 (run 27583523004, 2026-06-15 ~23:54Z):
all 3 jobs success (unit + check-in + pixelWrite #926).** This run closed the last
gaps the user flagged: (a) **check-in proof now anchors on the REGISTRATION HANDSHAKE,
not bare connectivity** — the protomq.log window shows device→broker
`{"machineName":"qtpy-esp32s3-n4r2","macAddr":...,"strVersion":"1.0.0-beta.130"}` →
broker→device `CreateDescriptionResponse {"response":"RESPONSE_OK","totalGpioPins":30,
"totalAnalogPins":4,"referenceVoltage":2.5,"totalI2cPorts":1}` → `RegistrationComplete
{"isComplete":true}` (serial side: "Registration and configuration complete!"), with
shared UTC-ms timestamps lining serial/protomq up; (b) **serial.log captured** (socat
installed + pause/resume) — populated section + `hil-serial-logs` per-log artifact;
(c) **MSC `write_secrets_msc` works** — needed **passwordless sudo, which the Pi 4
LACKED** (Pi 5 rpi-config grants it, Pi 4 does not). Granted on rpi-hil006 via
`/etc/sudoers.d/010-hil-nopasswd` (`pi ALL=(ALL) NOPASSWD: ALL`, visudo-checked) AND
baked idempotently into `scripts/setup-hil-host.sh`. Also shipped: git+gh install in
setup script + per-job host git/gh signout (firmware_bench `_signout_host_git` in
teardown) so PAT-cloned repos don't leak creds; per-log-type CI artifacts
(serial/protomq/flash/events + hil-assets bundle). Controller deployed @ **93e8f5d**;
sudo fix is host-script-only (no controller restart). PIPELINE IS DONE/PROVEN.

**✅ CLEAN 3/3 HIL run on rpi-hil006 (run #13, 2026-06-15 PM): check-in ok=true +
pixelWrite LOW rebooted=true / HIGH rebooted=false — the #926 regression proven on
the new host.** CI auto-routed there (rpi-displays in maintenance → available-DUT
selection picked rpi-hil006). Two issues fixed to get here: (1) the apt esptool on
the host shipped WITHOUT stub_flasher data (run_stub FileNotFoundError) → setup
script now installs esptool via **pip only, never apt**, verifying the stub data;
(2) `soft_reset` (no-solenoid power-cycle fallback) now prefers `--after
watchdog_reset` (native-USB reliable reboot) and falls back to `--after hard_reset`.
Controller deployed @ **4d29903**.

**rpi-hil006 added as a 2nd HIL host (2026-06-15 PM) — supplements, doesn't replace
rpi-displays.** It's a **Pi 4B** whose USB-A ports are on **xhci** (VL805/PCIe), NOT
dwc_otg — so it has none of rpi-displays' (Pi Zero 2W) wedge/self-reboot fragility;
much more stable. Provisioned via `scripts/setup-hil-host.sh pi <pubkey>` (groups,
usbip+usbipd:3240, esptool/udisks/usbip toolchain — now installed by the script
itself, idempotent; ModemManager absent; dwc2 left OFF/moot). SSH: controller key is
`/etc/hil/keys/rpi-hil-fleet` (a copy of the rpi-displays key); its pubkey is in
pi@rpi-hil006 authorized_keys. **rpi-hil006 sshd is pubkey-only** (the console/sudo
password does NOT work over SSH); reach it directly from the dev box via Windows
OpenSSH (`pi@rpi-hil006`, user's key) or controller→host via the fleet key.
DUT `mcu-qtpy-esp32s3-n4r2-hil006` (QT Py S3, MAC f4:12:fa:59:64:84, build_target
qtpy_esp32s3_n4r2, serial_port by-path `…pcie…usb-0:1.2.1.1:1.0`, busid 1-1.2.1.1,
NO solenoid → esptool reset, always-on power + data-only switchable hub). **Flash
chain validated** (1200-touch → esptool flash_id read the chip via the fleet key).
rpi-displays' qtpy (mcu-feather-eink-29-rbw) marked maintenance ~1h. CI now prefers
an AVAILABLE DUT among same-build_target devices (hil-lib.sh `_hil_fetch_target_rec`,
WS commit a598cee) so either host can serve the chip. Extensibility TODO: a per-device
`reset_method`/`power_control` field (none|esptool|solenoid:<ch>|uhubctl|usbip-rebind)
to first-class the data-hub + a future solenoid on rpi-hil006.

The GitHub-CI HIL pipeline works and is proven on real hardware (2026-06-14/15):
WS PR #930 (`adafruit/Adafruit_Wippersnapper_Arduino` branch `hil-test-additions`)
→ `usbip-hil-controller` on tachyon over Tailscale → QT Py S3 on rpi-displays.
Runs a **test array** (check-in gate ✅ + pixelWrite #926 A/B: LOW beta.127
`rebooted=true` / HIGH PR-fix `rebooted=false` ✅), **new PR comment per run** with
inline serial.log proof + `hil-assets` artifact.

Deployed: controller `main` @ **ae1f85e** (docs 2179e80) on tachyon,
`HIL_AUTO_HOST_REBOOT=true` in run/controller.env; CI on WS `hil-test-additions`
@ **1338f2c**. Shipped + live-validated: pipeline-from-params, DB hardware
overlay, msc_filter derivation, power-cycle disappear/reappear detection,
serial+MQTT reboot detection, timestamped logs, verify_checkin, and **dwc_otg
wedged-host auto-reboot recovery** (`host_recovery.py`).

**Resume from [`docs/HANDOFF.md`](../../docs/HANDOFF.md)** — full plan + next
steps: **(1) tolerate a DUT-host reboot BETWEEN the 3 test runs — DEPLOYED, one
re-run from proof.** Deployed: controller `main` @ **ee7180e** on tachyon; CI WS
`hil-test-additions` @ **4cbb1ccd** (PR #930). Controller advertises `retry_after
= now + HIL_HOST_REBOOT_ETA_S` (300s) on a wedge (`mark_host_wedged`) AND on a
network-unreachable stage error (`mark_host_unreachable`, no reboot — self-heals
via the new reconciler presence probe). CI `hil-lib.sh`: `wait_for_target_available`
before each test + drains trailing events + retries once on `is_infra_error(state)`.

**On-demand power + full-output capture (ee7180e, 2026-06-15 PM):** only the job's
solenoid channel is energised (firmware_bench `_power_on_dut`/`_power_off_dut`); idle
DUTs incl. a **bad/flaky board** stay off the bus (user: "bad device on the hub").
`reboot_host` = gentle sequential per-channel bring-up (one at a time + presence/dmesg
check → off), NOT the `all_on` storm — validated by hand to recover the QT Py (port
1.2; `-32` clears on retry). Presence probe dmesg-aware (`dmesg_usb_error_count`,
ignores benign "new … using dwc_otg"). `bench_stages.record` surfaces FULL
stdout+stderr per command (UTC-ms) to the CI feed, not a whitelist (capture was
always complete in flash.log; the live feed hid erase progress + esptool deprecation
warnings). Open: esptool v5 hyphenated syntax (erase-flash/no-reset) to drop the
warnings; on-demand probe should power-cycle to validate an idle-off device.

**FINAL RUN (2026-06-15 ~19:30, ee7180e→a57a618): check-in ✅ ok=true, pixelWrite
HIGH ✅ rebooted=false (the #927 fix proven graceful), pixelWrite LOW ❌ unknown.**
LOW failed ONLY because rpi-displays went unreachable right after check-in
(`port_on ch4 → [Errno 113] No route to host`) — correctly flagged + retried, but
the host didn't stabilise in LOW's budget; HIGH ran later once it was back. ROOT
CAUSE of the remaining flakiness: **rpi-displays spontaneously reboots itself under
HIL load** (uptime ~2min between runs; `vcgencmd get_throttled=0x0` so NOT
undervoltage; controller never issued the reboot — host status `available`, only a
"flagged unreachable (no reboot)"; journald volatile so no crash trace). It's the
known-weak 415MB Pi (see [[reference_rpi_displays_compute]]) — likely a dwc_otg
kernel fault/panic, a HOST problem, not the controller. The controller software +
ride-through is proven working; a clean 3/3 is blocked by host stability. Also a
gotcha to revisit: target→device label mapping looks scrambled
(`qtpy_esp32s3_n4r2` → device_id `mcu-feather-eink-29-rbw`) though jobs route to the
right physical board (port 1.2).

**Run #12 (04a6b747 multi-retry, ETA 150): check-in ✅ ok=true "(host rebooted —
retried x2)" — bounded multi-retry (HIL_TEST_ATTEMPTS default 4) PROVEN; pixelWrite
LOW unknown, HIGH skipped (host unreachable).** CONFIRMED root cause: rpi-displays
is STABLE when idle (27min uptime) but REBOOTS ITSELF UNDER HIL USB LOAD (13min
uptime mid-run; throttled=0x0 so NOT power; dwc_otg errors in dmesg). The weak
415MB Pi's dwc_otg/kernel can't sustain the flash/power-cycle/MSC USB load →
self-reboot. Software is MAXED + proven correct across runs (check-in via multi-
retry #12; pixelWrite HIGH rebooted=false #11 = #927 fix proven) — a clean
single-run 3/3 is blocked purely by host hardware stability. PATH FORWARD IS
HARDWARE: sturdier DUT host, or fix the Pi's dwc_otg kernel crash, or reduce USB
load. No further controller/CI change will stabilise an unstable host.

**Run #10 proved the check-in test rides through a wedge (waited 313s → ok=true);
pixelWrite then errored `No route to host` with no retry (fixed in a3c7071/4cbb1ccd).
Run #11: retry fired but still unknown — host unreachable during staging, unflagged
(fixed by efb1773 guarding the whole flash setup). NEXT: watch the on-demand re-run
of 27561303883 — expect all 3 tests to pass with the bad device isolated.**
Still open: out-of-band power recovery for a non-self-recovering host; the
*proactive* dmesg-storm half of the probe. (run #8/#10 failed `unknown` *only*
because the host went offline mid-run.)
(2) fully-down-host
out-of-band recovery (SSH reboot can't fix an L3-unreachable host; rpi-displays
self-recovers in 3–5 min); (3) dmesg presence probe (reconciler probe still a
stub); (4) full `pytest tests/` hangs (run targeted files). See
[[dwc-otg-wedge-hides-bench-reboot-fixes]] and [[project_firmware_bench]].
