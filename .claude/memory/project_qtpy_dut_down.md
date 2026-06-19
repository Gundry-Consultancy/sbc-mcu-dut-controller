---
name: dwc-otg-wedge-hides-bench-reboot-fixes
description: When DUTs vanish and won't re-enumerate via power-cycling, the rpi-displays dwc_otg USB stack is wedged — reboot the Pi, don't conclude the device is dead
metadata:
  type: project
---

**Lesson (2026-06-14):** the QT Py S3 DUT (`mcu-feather-eink-29-rbw`,
`qtpy_esp32s3_n4r2`) "disappeared" — it would not enumerate over USB even with
every solenoid channel powered (`all_on`), survived multiple `all_off`+drain
cycles, per-channel scans, and direct catch-and-flash attempts. I wrongly
concluded it was **hardware-dead**. It was not: **rebooting rpi-displays brought
it straight back** (app mode, `239a:8143`, at its normal by-path `usb-0:1.2:1.0`
= the DB value), AND surfaced devices that had also been hidden (a Pico W and a
CP2104). So the real fault was a **wedged `dwc_otg` USB stack** on the Pi Zero —
exactly the documented failure (see [[reference_rpi_displays_power]]):
`dwc_otg` is **not runtime-rebindable; only a Pi reboot clears it**.

**How to recognise it:** half the bench vanishes at once / a DUT won't
re-enumerate after any amount of solenoid power-cycling, while other devices look
fine. Don't trust a channel scan or by-path map taken in this state — under a
wedge, enumeration is degraded and by-paths shift (the QT Py appeared to be at
`1.1.2` and ch4 appeared to power nothing; both were artifacts — after reboot ch
map + `usb-0:1.2:` were correct again). A brief `303a:1001` (USB-JTAG ROM) blip
in dmesg during `all_on` is usually a healthy S3 booting (`303a`→`239a`), not the
missing DUT coming back.

**Fix:** `ssh` to rpi-displays (nested via tachyon) and `sudo reboot`; it's back
in ~55 s. Then `all_on`, settle ~12 s, recheck `/dev/serial/by-id/`. This is a
**fully remote** bench — no reseating/physical access — so reboot is the recovery
of last resort before declaring a DUT dead.

Bench power scripts on rpi-displays: `~/all_off.sh`, `~/all_on.sh`,
`~/turn_on.sh <ch>`, `~/turn_off.sh <ch>`. See [[project_firmware_bench]].
