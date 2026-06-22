---
name: project-samd51-bossac-flasher
description: SAMD51/SAM boards now have a real flasher (BossacFlasher) — supersedes "no SAMD flasher exists"; PyPortal Titano is a live bench target
metadata:
  type: project
---

Shipped 2026-06-20 (commit b6803ce on main, deployed to the Tachyon controller).
**Supersedes the earlier assessment that SAMD51 had no working flasher** (only
esptool/pio/noop shipped; `BossacFlasher`/`Uf2MscFlasher` were specced-but-unbuilt
M4 in `docs/ARCHITECTURE.md`).

**`BossacFlasher`** (`src/hil_controller/adapters/flashers/bossac.py`) drives
`bossac` (BOSSA, apt `bossa-cli`, binary `bossac`) for SAM/SAMD21/SAMD51:
- Bootloader entry = the SAME 1200-baud double-tap (`stty -F <port> 1200`) the ESP
  boards use → SAM-BA; confirmed via `bossac -i` (`is_in_bootloader`). No
  ROM/JTAG concept (so no esptool `default_reset` fallback — recovery is a
  solenoid power-cycle then re-touch).
- `flash()` = one `bossac --port <tty> -e -w -v -b -R --offset=<app>` (erase +
  write + verify + boot-from-flash + reset). **App offset 0x4000 (SAMD51) /
  0x2000 (SAMD21); a 0/None offset is coerced UP to app_offset** so an ESP-shaped
  `offset: 0x0` can't overwrite the bootloader.
- bossac's `-p` wants a **bare `ttyACMn`** (its POSIX backend prepends `/dev/`),
  so the flasher `readlink -f`s the by-path symlink (stable across the
  app↔SAM-BA flip) to the live tty before each call.

Wiring: `bench_stages.make_flasher("bossac")`, a bossac-aware `enter_bootloader`
stage branch, `bossac_offset` on `BenchContext` (fed from `params.bossac_offset`),
and `SAMD51_FLASH_STAGES` (`enter_bootloader → flash@0x4000 → power_cycle`).
`setup-hil-host.sh` installs `bossa-cli` (already present on rpi-displays 1.9.1).
firmware-bench UI got a `bossac` option (note: its `verify` checkbox hardcodes
esptool, so for bossac uncheck Erase/Verify — bossac folds them into flash).

**Live target: PyPortal M4 Titano** (`mcu-pyportal`) — re-seated on rpi-displays
port 1-1.1.4 / solenoid ch3 ([[reference-hil-bench-usb-topology]]), configured in
DB + topology: `flasher: bossac`, `build_target: adafruit_pyportal_m4_titano`
(the WipperSnapper PIO env), serial_port by-path, status available. Shows in
`GET /v1/targets` as available. This unblocks the PR #930 firmware-version
bisection on the Titano (test each version twice, full erase + reboot).

The Titano's UF2/SAM-BA **bootloader is the `uf2-samdx1` family** (NOT tinyuf2,
which is ESP/nRF/iMXRT only — has no pyportal/titano asset), already factory-
installed; a future update/recovery path would be a separate task. The UI's
"Install TinyUF2" action is ESP-only and does NOT apply to SAM boards.
