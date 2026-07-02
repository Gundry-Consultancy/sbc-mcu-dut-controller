---
name: hil-bench-recovery
description: "Diagnose and recover the HIL bench itself — device availability semantics (temporary vs permanent outages, the self-heal reconciler and its retry budget, why 'unavailable' flags can be STALE), solenoid power + BOOTSEL channel control, host-unreachable vs host-wedge classification, the presence probe's requirements, and the infra-error-vs-test-verdict discipline every CI harness must keep. Use when: /v1/targets reports devices unavailable that you believe are healthy, a job failed with a host/USB error rather than a test failure, a DUT won't enumerate, you need to power or BOOTSEL a channel by hand, or a bench host was rebooted/rewired. NOT for: authoring tests (hil-author-test), submitting jobs (hil-job-api), camera-server ops (hil-camera-proof)."
---

# hil-bench-recovery

The bench heals itself — up to a point. This skill is the map of where that
point is, and what to do on the other side of it.

## Availability model (read this before trusting `/v1/targets`)

Devices carry `status` + `unavailable_kind` (`temporary | permanent`) +
`unavailable_reason` + a retry budget. Sources:
`src/hil_controller/availability.py`, `availability_reconciler.py`,
`host_recovery.py`; docs: `docs/device-availability.md`.

- **temporary** — set automatically when a stage failure looks like a
  network-unreachable host or a USB wedge (`host_recovery.py`
  `UNREACHABLE_REASON_PREFIX`). The reconciler (every `HIL_AVAIL_RECONCILE_S`,
  default 30s) re-probes with backoff **up to `max_attempts` (3)**.
- **permanent** — never re-probed; a human statement ("not wired to bench").
- **The trap: exhausted retries freeze the flag.** After 3 failed probes the
  device is *never probed again* — `reason` text like "back ~150s" keeps
  rendering forever. Flags observed frozen for **17 days** (2026-06-15 →
  2026-07-02) while the hardware was healthy. If `/v1/targets` disagrees with
  reality, suspect the flag, not the bench.

### Un-freezing

Until the retry/rectify endpoint exists, reset the budget and let the
reconciler prove the device (it only heals what actually probes healthy):

```sql
-- on the controller host, against the live DB (HIL_DB_PATH):
UPDATE devices SET retry_attempts = 0, retry_after = NULL
WHERE status != 'available' AND unavailable_kind = 'temporary';
```

Then watch `/v1/targets`: active probes are serialized and power-cycle each
channel (~30–60s per device), so a full bench takes minutes, not one tick.

### What the presence probe needs (or it can never pass)

The probe SSHes the host and checks the device's node: `serial_port` first,
falling back to `hub_port_path`. Two failure modes that look like dead
hardware but are data problems:

1. **`serial_port` NULL, only a busid** (`1-1.1.3`): the probe does
   `test -e 1-1.1.3` — never true. Fix: set `serial_port` to the real by-path
   node, pattern `/dev/serial/by-path/platform-<usbctrl>.usb-usb-0:<port>:1.0`
   (busid `1-1.1.3` → port `1.1.3`). Confirm by powering the channel and
   listing `/dev/serial/by-path/` on the host.
2. **SBC DUTs** have no USB node at all — the probe returns false forever.
   Restore them directly (`status='available'`, clear the unavailable columns)
   once you've confirmed host SSH works.

Devices with a `solenoid_channel` are probed **actively** (power on → await
enumeration ≤25s → dmesg clean → power off); channel-less MCUs are checked
passively and are expected to be always-powered.

## Power, BOOTSEL, and hands-on channel control

Each bench host has MCP23017-driven solenoid banks (16 ch, A/B). Controls,
most- to least-preferred:

- **Web UI** usb-ip page: per-host On / Off / Cycle per channel.
- `scripts/solenoid_hub_cli.py` — same from a shell.
- On the host itself: `~/turn_on.sh <ch>` / `~/turn_off.sh <ch>` (these are
  what jobs and probes call).

RP2040/Pico boards add `bootsel_channel` (+ `bootsel_inverted` when the
mechanical rig presses BOOTSEL on relay-OFF): flashing them is BOOTSEL-held
power sequencing, not an esptool bootloader touch. Native-USB-JTAG ESP32-S3
boards holding a latched I2C mux need `power_cycle` with
`reset_via: "esptool"` — a soft reset that keeps the solenoid mapped (see
hil-i2c-strands).

## Host-level failures

- **unreachable** (`No route to host`, SSH drop mid-stage): the host's devices
  get flagged temporary; hosts usually self-recover (Pi reboot ≈3–5 min for a
  dwc_otg wedge). `HIL_AUTO_HOST_REBOOT` (default false) gates automatic
  reboots; when it's off, recovery is: fix/reboot the host, then un-freeze
  flags as above.
- **USB wedge** (dmesg storms: `error -110`, `Timed out waiting for FSM`,
  `device not accepting address`): the recovery sequence is all-channels-off →
  reboot → gentle one-channel-at-a-time bring-up (avoids re-wedging dwc_otg).
- The controller's SSH uses per-host keys from the topology's `ssh_key_path`
  (e.g. `/etc/hil/keys/rpi-hil-fleet`) with known_hosts **disabled** — "I can
  ssh from my shell" and "the service can" are independent facts; test with
  the service's exact key, and vice versa a CLI known-hosts prompt failure
  does NOT mean the service is blocked.

## Infra error ≠ test verdict

The one discipline that keeps bisects and CI gates honest: a failure caused by
the bench is a **retry**, never a pass/fail data point.

- Infra: can't flash, host unreachable mid-job, USB never re-enumerated,
  broker didn't start. → recover (above), re-run the same ref/build.
- Verdict: flashed fine but the app misbehaves (no check-in, crash, wrong
  output). A build that flashes but never connects IS a valid "broken".
- `examples/wippersnapper-arduino/hil-lib.sh` implements the CI side:
  `wait_for_target_available` (bounded wait riding through a host reboot,
  default 360s) and `is_infra_error` (classify before you count). Copy it.

## Quick triage order

1. `GET /v1/targets` — read `kind` + `reason`, note *how old* the story sounds.
2. Host reachable? (`ssh pi@<host>` with the fleet key.) If yes and flags say
   otherwise → frozen flags: reset the retry budget.
3. Device really absent? Power its channel by hand, watch
   `/dev/serial/by-path/` and `dmesg` on the host.
4. Node mismatch? Compare what enumerated against the DB's `serial_port`.
5. Only then suspect the DUT hardware itself.
