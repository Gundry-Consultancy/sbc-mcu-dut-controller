---
name: hil-i2c-strands
description: "Route a shared I2C component strand (a chain of sensors, optionally with an on-strand TCA9548 mux routing among them) to exactly ONE DUT at a time via the ADG729 analog strand-mux (the sbc-dut-analog-mux-api CircuitPython box, break-before-make) and drive the components from a HIL job. Covers: the strand model (strands / strand_components / device_strands), requesting components via target.requires kind=i2c_strand — by capability tags OR component model short-names, case-insensitive — with the auto-prepended select_i2c_strand stage, explicit select_i2c_strand / isolate_i2c_strand stages, strand CRUD (/v1/strands + web form), the mux box's own HTTP API for manual poking. Use when: testing I2C sensors shared between multiple DUTs, adding/editing a strand, jobs that need specific sensors present. NOT for: displays (hil-camera-proof / hil-display-pytest) or plain non-muxed I2C sensor tests (hil-author-test)."
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
- WipperSnapper V1 doesn't support muxes, so if needed to test a component on
  an I2C mux channel (the i2c mux is part of a strand), where the mux channel
  is not active then it's possible to use a different DUT (e.g. the SBC host)
  to temporarily take the strand and then fire the i2c mux channel change
  command before returning the strand to the original DUT for WipperSnapper V1.
