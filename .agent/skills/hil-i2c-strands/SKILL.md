---
name: hil-i2c-strands
description: "Route a shared I2C component strand (a chain of sensors, optionally with an on-strand TCA9548 mux routing among them) to exactly ONE DUT at a time via the ADG729 analog strand-mux (the sbc-dut-analog-mux-api CircuitPython box, break-before-make) and drive the components from a HIL job. Covers: the strand model (strands / strand_components / device_strands), requesting components via target.requires kind=i2c_strand — by capability tags OR component model short-names, case-insensitive — with the auto-prepended select_i2c_strand stage, explicit select_i2c_strand / isolate_i2c_strand stages, strand CRUD (/v1/strands + web form), the mux box's own HTTP API for manual poking, and an appendix on flashing the native-USB-JTAG QT Py ESP32-S3 WITHOUT dropping a latched mux channel (power_cycle reset_via=esptool; tinyuf2 + UF2-swap + stty->esptool for CircuitPython<->WipperSnapper). Use when: testing I2C sensors shared between multiple DUTs, adding/editing a strand, jobs that need specific sensors present, or mux-preserving firmware swaps. NOT for: displays (hil-camera-proof / hil-display-pytest) or plain non-muxed I2C sensor tests (hil-author-test)."
---

# hil-i2c-strands

One sensor chain, many DUTs: a **strand** is a shared I2C component chain whose
SDA/SCL are physically routed to exactly **one DUT at a time** by an analog
strand-mux. Jobs declare *what components they need*; the controller picks a
DUT that can receive a strand providing them and muxes it on before the app boots.

## The strand model

Two levels of muxing, don't conflate them:

1. **Analog strand-mux** (ADG729 / ADG728-pair) — routes the *whole strand* to
   one DUT. This is the `sbc-dut-analog-mux-api-circuitpy` box (an `aux` in the
   topology), **break-before-make**: every switch opens before the single target
   route closes, so two DUTs never drive the shared bus at once.
2. **On-strand I2C mux** (TCA9548, e.g. @0x70) — routes among the strand's *own
   components* (modelled per-component as `tca_channel`; `null` = direct bus).
   Driven by the inject stages, not the strand-mux.

Controller tables: `strands` (id, `mux_aux` → the aux whose `interface` is the
mux-box base URL, `mux_group`, `tca_address`, pool/status) →
`strand_components` (model, address, `tca_channel`, `ws_types`, `capabilities`) →
`device_strands` (per-DUT analog-mux channel = the `routes` list). The **DB is
the source of truth** (edited via `/v1/strands` and the web form);
`deploy/topology.strands.example.yaml` is the reseedable shape and
`GET /v1/topology/export` backports the live DB into it. Gotcha: the
*scheduler's* strand-capability index is built from the topology file at
registry load, while *stage-time* route resolution queries the DB — keep them
in sync (export + reseed / restart after structural edits).

## Requesting a strand from a job

```jsonc
"target": {
  "requires": [ { "kind": "i2c_strand",            // "strand"/"component" also accepted
                  "capabilities": ["sensor:voc", "sgp41"] } ]
}
```

- Matching is **case-insensitive** against the union of each strand's
  components' `capabilities` tags **and their model short-names** (`"sgp41"`,
  `"pmsa003i"` work without any capability tagging).
- **One strand must cover ALL required capabilities** (only one strand can be
  muxed onto a DUT at a time). Devices with no covering routed strand are
  skipped during selection.
- On match the adapter **auto-prepends** `select_i2c_strand {strand_id}` as
  stage 0 — the chain is connected *before the DUT boots its app* — unless you
  already placed a select stage explicitly.

Explicit stages (see **hil-job-api** for the full stage vocabulary):

| stage | params | notes |
|---|---|---|
| `select_i2c_strand` | `{strand_id}` or explicit `{base_url, group, channel}` (+`token`) | resolves the route from the DB (strand → aux → per-device channel); errors if this device has no route |
| `isolate_i2c_strand` | `{strand_id}` or `{base_url}` | opens every switch — strand connected to nothing |

Both log a greppable `I2C_STRAND_MUX_VERDICT status=ok|error|isolated …` line.

Once routed, drive the components over the broker: `inject_i2c_probe` /
`inject_i2c_scan_v1` for presence, `inject_i2c_settings` for
Add-with-settings + readings (`I2C_SETTINGS_VERDICT` per test). Per-component
`mux_channel` there refers to the **on-strand TCA**, and its `mux_address`
defaults to **0x77** — pass it explicitly when the strand's TCA differs (the
rpi-hil006 air strand's is `0x70`).

## Strand CRUD

- `GET /v1/strands` (list) · `GET /v1/strands/{id}` · `POST /v1/strands` (201,
  409 if exists) · `PUT /v1/strands/{id}` (upsert; body id must match path) ·
  `DELETE /v1/strands/{id}`.
- Writes are **declarative**: a PUT *replaces* the strand's whole components +
  routes lists — send the full object, not a delta.
- The web UI has an equivalent strand form.

## The mux box itself (manual poking)

The strand-mux is a CircuitPython ESP32-S3 running
`sbc-dut-analog-mux-api-circuitpy` — on this bench at **`http://192.168.1.155:8080`**
(aux `mux-hil006`), with a self-contained web UI at `/`. Useful endpoints:
`GET /api/status` (active DUT + all switch states), `POST /api/select
{"dut":"<name>"}` (break-before-make), `POST /api/isolate`,
`GET/PUT /api/topology` (validated before applied; bad payload = 400, hardware
untouched), `GET /api/probe` (which switch chips ACK on the control bus).
Bearer auth only if the box's `API_TOKEN` is set.

Gotchas:
- The box **latches its last selection** — a selection survives controller
  restarts and idles indefinitely. The bench currently latches **dut-01 / ch0
  when idle**, so "nothing selected anything recently" ≠ "isolated": check
  `/api/status`, don't assume.
- Saving topology at runtime hits CircuitPython's one-writer filesystem rule: a
  `/host-writeable` marker decides whether USB-host or the device may write;
  toggling it needs a **hard reset** (`POST /api/reboot`). A topology change is
  still *applied* to hardware even when the save fails (`"saved": false`).
- No control-bus pull-ups at boot → the server starts **degraded** (switching
  returns 503; status/topology/reboot still work).

## Worked example: the rpi-hil006 air-quality strand

`strand-hil006-air` (see `deploy/topology.strands.example.yaml`): one ADG729
(group `muxA`, chip @`0x44` on the mux box's control bus) shares an air-quality
chain among **4 DUTs** — 3 Arduino MCUs + **the Pi itself as an SBC DUT** (its
own I2C bus is muxed in):

- components: `pmsa003i` @0x12 direct on the strand bus
  (`sensor:pm10/pm25/pm100`), `sgp41` @0x59 behind the on-strand TCA9548 @0x70
  ch0 (`sensor:voc`, `sensor:nox`).
- routes: ch0 `mcu-qtpy-esp32s3-hil006`, ch1 `mcu-feather-esp8266-hil006`,
  ch2 `mcu-lilygo-tdisplay-s3-hil006`, ch3 `sbc-rpi-hil006-self`.

A job asking `{"kind":"i2c_strand","capabilities":["sensor:pm25"]}` (or just
`["pmsa003i"]`) matches this strand, lands on any of the four routed DUTs, and
gets `select_i2c_strand strand-hil006-air` prepended automatically.

## Appendix: flashing without dropping the mux (QT Py ESP32-S3)

Mechanics for the native-USB-JTAG **QT Py ESP32-S3 N4R2**
(`mcu-qtpy-esp32s3-n4r2-hil006` on **rpi-hil006**), whose sensor chain sits
behind a TCA9548A @0x77. Full detail + sensor inventory:
[`docs/qtpy-mux-i2c-hil.md`](../../docs/qtpy-mux-i2c-hil.md).

**The core problem.** No UART bridge — esptool only reaches the ROM download
loader (`303a:1001`) if the *running firmware* flips into it on a 1200-baud
touch. **WipperSnapper/Arduino and tinyuf2 honor the touch; CircuitPython does
NOT** (its TinyUSB CDC ignores the touch *and* esptool's reset). A TCA9548
channel latch only resets on a **power cut** — so any flash that power-cycles
via the solenoid loses the latch.

**Controller path (preferred).** `power_cycle` with `reset_via: esptool` does a
soft reset **without unmapping the solenoid** — a latched TCA channel survives
a full controller flash→secrets→checkin pipeline. Without it, a device with
`solenoid_channel` set gets a real power cut (mux reset).

**tinyuf2 + soft swaps (CircuitPython↔WipperSnapper).** Flash the **tinyuf2
bootloader** once; then all swaps are soft (no power cut) → mux held:

1. **CP sets the mux channel**, then hands to tinyuf2:
   ```python
   import board, microcontroller
   i2c=board.STEMMA_I2C(); i2c.try_lock(); i2c.writeto(0x77, bytes([1<<CH])); i2c.unlock()
   microcontroller.on_next_reset(microcontroller.RunMode.UF2); microcontroller.reset()  # -> tinyuf2 QTPYS3BOOT
   ```
2. **Swap firmware**, either route:
   - **UF2 drag-drop** (CP→WS): mount `QTPYS3BOOT` (`/dev/sda`), `cp ws.uf2`, `sync` → WS boots.
   - **stty→esptool** (from WS or tinyuf2, NOT CP): `stty -F <port> 1200` → `303a:1001`, then
     `esptool --chip esp32s3 --port <port> --before no-reset --after watchdog-reset <op…>`.
3. WS comes up with the channel still selected.

`RunMode.UF2` works **only because tinyuf2 is present** — esptool-flashing a
*combined* `.bin` at 0x0 **overwrites tinyuf2**; install CP/WS via the **UF2
drive** (or esptool only the app partition) to keep it.

**Reliable flasher = the controller.** `firmware-bench` flashes via the
1200-touch and recovers wedged boards via power-cycle +
`force_download_via_reset` (fix `f425ed0`: no `boot_settle` before the catch —
CP boots in ~1.6s, the ROM window was closing first). It's the way out of a
CP/wedged state; a full flash→secrets→checkin is proven on this board (WS
`2.0.0-alpha.1`, broker `R_OK`). For mux-preserving flows keep every
`power_cycle` on `reset_via: esptool`.

**Gotchas** (all hard-won):
- USB VID:PID = mode: `239a:8143`=WS, `239a:8144`=CircuitPython,
  `239a:0143`=tinyuf2 (`QTPYS3BOOT`), `303a:1001`=ROM download (esptool),
  `303a:4001`=MicroPython.
- **Never base64 binaries over SSH** — `scp` / host-side `curl`. Fleet SSH =
  git-bash `/usr/bin/ssh -i /tmp/hilkey pi@rpi-hil006`; tachyon = Windows
  OpenSSH `particle@192.168.1.169`.
- Be **patient after a reset** — the by-path port briefly vanishes; poll ~15-30s
  before declaring failure.
- Recover a wedged board: `~/turn_off.sh 0 8 && ~/turn_on.sh 0` — a real power
  cut, **the mux latch resets**. If the **Pi's** xhci wedges after an error
  storm, `sudo reboot` rpi-hil006.
- High-current sensors (SCD30, PMSA003I) can brown out re-enumeration — remove
  if flaky.
