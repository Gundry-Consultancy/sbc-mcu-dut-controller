---
name: msc-mount-udisksctl-noise
description: write_secrets_msc udisksctl NotAuthorized = a polkit misconfig (NOT benign); fixed by a polkit rule + USB-autosuspend udev rules the bench hosts need
metadata:
  type: reference
---

**`write_secrets_msc` mount path** (`src/hil_controller/adapters/msc_secrets.py`
`_mount_msc`): tries `udisksctl mount -b <dev>` first, then falls back to
`sudo mount -t vfat -o rw,uid=...,gid=... <realdev> /tmp/hil-msc-<name>`.

**The `udisksctl … NotAuthorizedCanObtain` warning is NOT benign** (user, 2026-06-16:
"any warnings are signs of trauma"). Over SSH there is **no active seat session**, so
the default udisks2 polkit policy (`filesystem-mount` → `auth_admin` for inactive
sessions) denies it; the `sudo mount` fallback then masks the misconfig. Confirmed by
`pkcheck --action-id org.freedesktop.udisks2.filesystem-mount --process $$ -u` → rc!=0.
**This was the SAME on rpi-displays** (it also lacks a polkit rule and falls back to
sudo) — so it was never a rpi-hil006-vs-rpi-displays difference, contrary to first guess.

**Proper fix (shipped in `scripts/setup-hil-host.sh`, applied to rpi-hil006 2026-06-16):**
a polkit JS rule `/etc/polkit-1/rules.d/50-hil-udisks.rules` granting the udisks2
filesystem-mount actions to the **plugdev** group. KEY GOTCHA: a seatless SSH caller
makes udisks2 escalate the mount to **`filesystem-mount-other-seat`** — granting only
`filesystem-mount`(+`-system`) is NOT enough (udisksctl still fails; `pkcheck` for the
bare `filesystem-mount` passing is a red herring). Use a PREFIX match on
`action.id.indexOf("org.freedesktop.udisks2.filesystem-mount") === 0` (covers mount,
-system, -other-seat, -no-policy) + `filesystem-unmount-others`. CONFIRMED (run
27586759035): udisksctl mount → **exit 0**, mounts at `/media/pi/WIPPER` (rootless,
no sudo fallback), zero NotAuthorized / textual-auth-agent / could-not-open-port lines.
(Debian 12+/trixie polkit uses JS rules in `/etc/polkit-1/rules.d/`, not `.pkla`. The
bench user must be in `plugdev` — setup script already adds it.) NOTE: `udevadm trigger`
defaults to `change` events; the autosuspend rule matches `ACTION=="add"`, so apply it
live with `udevadm trigger --action=add --subsystem-match=usb` (no reboot needed).

**The REAL rpi-displays parity gap was udev rules** (rpi-hil006 was missing all three;
now added to the setup script + applied):
- `99-usb-no-autosuspend.rules` — `ATTR{power/control}="on"` on hub VIDs (Genesys 05e3,
  VIA 2109) + native-USB MCU VIDs (Adafruit 239a, Espressif 303a). **This is the likely
  root of the USB traumas** (esptool "No serial data received", serial-capture "could not
  open port … No such file", flaky native-USB-S3 recovery after a crash): without it the
  DUT's serial/JTAG/MSC endpoints autosuspend and vanish mid-job. Rule fires on
  `ACTION=="add"`, so `udevadm trigger --action=add --subsystem-match=usb` applies it
  without a reboot (plain `udevadm trigger` sends `change` and won't match).
- `99-usb-serial.rules` — dialout/0664 on serial VIDs (303a/239a/10c4/1a86/0403).
- `98-Picotool.rules` — RP2040 (2e8a) perms for future Pico DUTs.

See [[hil-ci-pipeline-state]] and [[reference_hil_bench_usb_topology]].
