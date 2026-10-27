# Board rescue — design note

Recover a wedged / dead-firmware DUT **through the normal job queue** (leased,
logged, asset-captured), instead of ad-hoc SSH poking. The discipline is
non-destructive first: **probe → bootloader entry → erase/reflash**.

## Phase 1 (implemented)

Four firmware-bench stages (`adapters/bench_stages.py`), a picotool flasher,
and a web Rescue page:

- **`serial_probe`** — a one-shot pyserial **write** primitive
  (`serial_capture.write_serial_bytes`, run as `python3 -c` on the DUT host —
  serial was read-only before this) pokes the console (enter / ctrl-C / ctrl-D /
  raw hex), coordinating with the running capture via the ctx
  `pause_serial`/`resume_serial` hooks so the writer and socat never share the
  tty. The response window is read back from the controller-local `serial.log`
  and classified: `circuitpython_repl`, `circuitpython_safe_mode`,
  `micropython_repl`, `boot_banner_detected`, `silent`, `other`. Classification
  + byte count go to the flash.log transcript; the raw capture is the
  `serial-probe.log` job asset. Optional `expect` regexes make it an assertion.
- **`enter_bootsel`** — the web BOOTSEL route's hold → power-cycle → release
  choreography, refactored into `SolenoidHubAdapter.bootsel_entry` (honours
  `bootsel_inverted`; release in a `finally`) and shared by the route and the
  stage. Needs `devices.bootsel_channel` (now plumbed onto `ctx.device` via the
  registry's device SELECT) — a clear StageError otherwise.
- **`picotool_rescue`** — `adapters/flashers/picotool.py` runs `picotool` on
  the DUT host (`info -a` / `reboot -f -u` / `load -v -x` / `erase` / `reboot`).
  For `load`/`info`, "No accessible RP-series devices in BOOTSEL mode" triggers
  one `enter_bootsel` + retry when the device has a `bootsel_channel`
  (`bootsel_fallback`, default true). **picotool (>= 2.0 for erase) must be
  installed on bench hosts owning RP2040/RP2350 DUTs.** Registered in
  `BenchContext.make_flasher("picotool")`.
- **`esptool_rescue`** — thin orchestration over `EsptoolFlasher`: the full
  `enter_bootloader` detect→rectify ladder first (1200-touch → USB-JTAG reset →
  hub power-cycle), then `chip_id` / `read_mac` / `erase_flash` / `write`
  (staged firmware at `address`, reusing the flash stage's retry + recovery).

Ergonomics: `params.bare: true` suppresses the `launch_protomq` auto-insert
(start_serial_log / print_boot_log inserts are kept) so rescue jobs carry no
app infrastructure; `RESCUE_PROBE_STAGES` is the documented starter list. The
web page `/ui/rescue/{device}` submits each action as a bare job and redirects
to the job page. Stage vocabulary: `docs/api.md`; triage order:
`.agent/skills/hil-bench-recovery`.

## Phase 2 (planned): CircuitPython miniesptool UART flasher

For ESP32 DUTs whose **USB is dead** (blown CDC, broken connector, wedged
USB-Serial/JTAG) the remaining way in is the UART0 ROM loader. Plan:

- A bench **CircuitPython board** (its own USB on the bench host) cross-wired
  to the dead target: TX/RX ↔ target **UART0**, plus GPIOs to the target's
  **EN** and **IO0** so the helper can drive the classic "IO0 low + EN pulse"
  download-mode entry in hardware.
- The controller drives the helper over its **CircuitPython console** (the
  `write_serial_bytes` primitive + capture already give it a REPL channel) to
  run **`adafruit_miniesptool`**: sync, chip detect, erase, write the staged
  image in chunks, verify MD5.
- A new stage (working name `miniesptool_rescue`) mirroring `esptool_rescue`'s
  actions, with the firmware chunked through the helper board.

**Blocked on bench wiring** (no cross-wired helper board is installed yet).
The **topology will model the cross-wiring**: the helper is a device of its
own, and the UART0/EN/IO0 harness becomes explicit connections (digital-twins
`connections` rows / a `rescue_uart` aux) from helper pins to target pins, so
jobs can require "a rescue helper wired to this DUT" the same way they require
an I2C strand today.
