---
name: reference-rpi-displays-compute
description: rpi-displays compute limits — 415MB RAM (swaps under compile) and /tmp is a 208MB tmpfs; too weak to compile WipperSnapper locally
metadata:
  type: reference
---

Bench host `rpi-displays` (192.168.1.234) compute/storage reality, discovered
2026-05-27 trying to build Adafruit WipperSnapper for the Feather ESP32-S3
([[reference-rpi-displays-power]] covers its USB/power limits).

**RAM:** only ~415 MB total + ~415 MB swap. A PlatformIO ESP32 build with the
default `-j4` thrashes swap and risks OOM (`cc1plus` ~100–300 MB each). Use
`pio run -j1` if you must build here. Realistically this host is **too weak to
compile WipperSnapper** in reasonable time — prefer building on the controller
(Tachyon, 8-core/multi-GB) per [[project-exec-location-feature]].

**/tmp is tmpfs, 208 MB, RAM-backed.** The controller hardcodes the job
work_dir to `/tmp/hil/{job_id}` (`git_deploy.py:55`); a real build (toolchain +
`.pio` + node_modules) does NOT fit in 208 MB → `pip install` dies with
`[Errno 28] No space left on device`. `/` (mmcblk0p2) has ~11 GB free.
**Workaround applied (does not survive reboot):** bind-mounted `/tmp/hil` onto
disk: `sudo mount --bind /home/pi/hil-work /tmp/hil` (target chowned `pi`,
sticky bit). Durable fix = make the controller work base disk-backed +
configurable (e.g. `HIL_WORK_DIR`) instead of `/tmp`.

**~/.platformio caching:** the ESP32 toolchain + arduino framework + IDF libs
(~5 GB) install into `~/.platformio` (persists across jobs); only `.pio/libdeps`
(per-workspace git clones of `lib_deps`) is wiped with the work_dir. So a warm
rebuild skips the multi-GB downloads but re-clones libdeps + recompiles.

**How to apply:** Don't expect rpi-displays to build firmware; route compiles to
the controller. If a job must build here, ensure /tmp/hil is disk-backed and use
`-j1`.
