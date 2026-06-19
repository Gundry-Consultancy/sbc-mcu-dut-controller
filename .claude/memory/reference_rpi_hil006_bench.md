---
name: rpi-hil006-bench
description: rpi-hil006 (Pi 4B) microcontroller bench — solenoid hub @0x20, CSI imx708 camera on :8080, channel map (QT Py S3 ch0, Feather HUZZAH ESP8266 ch5)
metadata:
  type: reference
---

**rpi-hil006 = a 2nd full microcontroller bench** (Pi 4B / xhci-USB), provisioned
2026-06-17 to match rpi-displays. SSH: `pi@rpi-hil006` (Windows OpenSSH, user key; fleet
key for the controller). NOTE: after a reboot the hostname `rpi-hil006` can be slow to
resolve — it has also answered as rpi-hil002 / rpi-hil004 transiently.

**Solenoid hub:** Adafruit 8-ch driver, MCP23017 at **I2C 0x20 on bus 1** — the SAME as
rpi-displays, NOT a different address (the "different address" expectation was wrong; both
are 0x20). `solenoid_hub_cli.py` now takes `--i2c-address` / `$HIL_SOLENOID_I2C_ADDRESS`
(default 0x20) anyway. CLI+driver deployed to `/opt/hil/`; Blinka + adafruit-circuitpython-
mcp230xx installed (pip --break-system-packages). pi is in the `i2c` group (no sudo needed).

Blinka is in a **venv** `/opt/hil/venv` (`python3 -m venv --system-site-packages`), NOT
`pip --break-system-packages` (user directive). `solenoid_hub_cli.py` auto-re-execs under
`/opt/hil/venv/bin/python` (`$HIL_SOLENOID_VENV` override, exec-loop guarded) so the
controller keeps calling plain `python3 /opt/hil/solenoid_hub_cli.py` — no controller-side
python-path config. Camera deps are apt (`python3-picamera2`), not pip.

**Channel ↔ DUT map (clean-baseline sweep 2026-06-17: all_off then per-channel port_on):**
- **ch0 → QT Py ESP32-S3 N4R2** (239a:8143, native USB-Serial/JTAG), sysfs `1-1.2.1.1`,
  by-path `…pcie…usb-0:1.2.1.1:1.0`, build_target `qtpy_esp32s3_n4r2` (in DB, not seeded).
  ⚠️ **Kept ALWAYS-ON (no solenoid_channel)** — the latching power control read back
  INCONSISTENTLY on re-probe (QT Py persisted through all_off), and this is the live PR
  #930 CI DUT, so on-demand power is DEFERRED until the channel map is reverified. Don't
  set solenoid_channel:0 on it until the latch behaviour is reliable.
- **ch5 → Feather HUZZAH ESP8266** (the "2nd Arduino"; esptool: ESP8266 4MB, CP2104 bridge
  ser **028570A4**, MAC c8:c9:a3:90:31:eb, 10c4:ea60), sysfs `1-1.2.3` (Port 3 of the 1-1.2
  hub), serial by-id `usb-Silicon_Labs_CP2104_USB_to_UART_Bridge_Controller_028570A4-if00-port0`.
  build_target = `huzzah` (WS PIO env; `feather_esp8266` platform) — set via inventory UI.
  CP2104 DTR/RTS → EN/GPIO0, so esptool `hard_reset` works (no 1200-touch).

**Power control is RELIABLE — earlier "OFF is flaky / latch inconsistent" was WRONG.**
That whole reliability investigation ran against a BROKEN CLI: the solenoid venv had no
Blinka (see below), so every CLI call no-op'd and the DUTs never moved — I misread that
as flaky latching/OFF. With Blinka actually in the venv, an `all_on` / 5s / `all_off` /
5s loop reliably powers both DUTs up and clears them on `all_off` (verified visually +
via enumeration). all_on/all_off via the CLI is the dependable primitive; don't add
verify-retry power control or defer on-demand power on the strength of the bogus numbers.

**VENV GOTCHA (root cause of the above):** `/opt/hil/venv` is `--system-site-packages`,
so `pip install adafruit-blinka` sees a system/user copy as already-satisfied and installs
NOTHING into the venv — it then relies on that external copy and breaks when it's removed.
FIX (in `scripts/setup-hil-host.sh`): `pip install --ignore-installed adafruit-blinka
adafruit-circuitpython-mcp230xx` to force them INTO the venv, + a post-install assert that
`board.__file__` is under `/opt/hil/venv`. Verify with
`/opt/hil/venv/bin/python -c "import board;print(board.__file__)"` → must be the venv path.

**Deployed to tachyon 2026-06-17** (controller restarted + seeded): csi-rpi-hil006 camera +
rpi-hil006 capabilities + both DUTs in the live DB (QT Py hub_port_path 1-1.2.1.1, camera_id
added, build_target preserved). QT Py left ALWAYS-ON in the DB only out of caution during the
broken-CLI confusion — now that power control is confirmed reliable, it CAN be moved to
solenoid ch0 / on-demand power (user's original goal) when desired.

**CSI camera:** Raspberry Pi **Camera Module 3 (imx708)**, autofocus, on bus 22 (0x1a). Served
by `tools/camera-server/server.py` (picamera2 backend, `--no-neopixel` — no ring) as
`hil-camera.service` on **:8080**; registered as camera `csi-rpi-hil006`
(`http://rpi-hil006:8080/`). Needs `python3-picamera2`.

**`build_target` is NOT seeded from topology.yaml** (the seeder skips it; it's operator-set
inventory metadata). So the ESP8266's build_target must be set via the admin/inventory UI/API;
adding to topology.yaml alone won't make it CI-testable. See [[hil-ci-pipeline-state]],
[[reference_hil_bench_usb_topology]], [[msc-mount-udisksctl-noise]].
