---
name: reference-rpi-displays-power
description: rpi-displays USB/power-control reality — uhubctl can only toggle the root port, not the Genesys hub's per-port power
metadata:
  type: reference
---

Bench host `rpi-displays` (192.168.1.234, ssh `pi`, key `/etc/hil/keys/rpi-displays`,
reached via [[project-deployment]] jump from `particle@192.168.1.169`).

**USB tree:** root hub → Genesys `05e3:0610` hub (Dev 002, 4-port) → second Genesys
`05e3:0610` hub (Dev 003, 4-port). Both always enumerate.

**uhubctl limitation (non-obvious):** `sudo uhubctl` lists ONLY the root hub
(`1d6b:0002`, 1 port, ppps) as power-controllable. The Genesys `05e3:0610` hubs report
**"ganged"** and uhubctl will NOT switch their downstream ports — `uhubctl -l 1-1` →
"No compatible devices detected". So `uhubctl -a on/off` is all-or-nothing on the whole
hub tree; there is NO per-port USB power switching via uhubctl on this bench, despite the
operator's expectation of "powering ports one at a time".

**Consequence:** A DUT (e.g. `mcu-feather-esp32s3-revtft`) that is not enumerating
(`no /dev/ttyACM*`, lsusb shows only the two hubs) cannot be brought up remotely by me.
Device power on this bench is gated by something uhubctl doesn't control (MCP23017
solenoid hub / external supply per [[project-deployment]]) and/or needs physical action.
esptool 5.2.0 IS installed on the Pi for chip-id probing once a port is live.

**Why:** Discovered 2026-05-27 while trying to complete arduino-ws job
c98389ff (failed at flash; device absent from USB).
**How to apply:** Don't promise remote device power-cycling on rpi-displays via uhubctl.
If a DUT isn't on the USB bus, surface it as an operator/hardware blocker.
