# HIL farm: API shape and tooling — investigation, 12 October 2026

Design note only. Nothing in the controller, the bench, or any downstream repo
was changed. It builds on the September
[job payload audit](job-payload-audit-2026-09-17/report.md) and its
[requirements/scenarios follow-up](job-payload-audit-2026-09-17/requirements-and-scenarios.md)
(both currently live in the main checkout, not on this branch), on Brent's
"WipperSnapper MVP: CI/CD build farm" to-do (9 October), on controller PRs
[#4](https://github.com/Gundry-Consultancy/sbc-mcu-dut-controller/pull/4)
(esp-usbip-bridge hosts) and
[#7](https://github.com/Gundry-Consultancy/sbc-mcu-dut-controller/pull/7)
(digital twins), and on a fresh read of the external tooling the user asked
about: hathach's tinyusb HIL harness, tannewt's CircuitPython HIL branch and
its companion firmware, the Wokwi client/CLI protocol, pytest-embedded and
labgrid. Every external claim below is pinned to a commit or version in
§2.

## 1. Recommendation in one page

Keep **one requirements contract** (the audit's typed `target` model) and add
**one live-control contract** (a Wokwi-shaped command/event session on a
running job). Everything else is a frontend or a backend of those two:

```
                 ┌───────────────────────────── frontends ─────────────────────────────┐
                 │  REST (POST /v1/jobs)   pytest-hil markers   hil CLI   GitHub Action  │
                 └───────────────┬──────────────────────────────────────────────────────┘
                                 │  requirements  (board/sensor/roles/requires)  ─ one schema
                 ┌───────────────▼──────────────────────────────────────────────────────┐
                 │  controller: validate → allocate (atomic lease) → run → assets        │
                 │  stages == recorded session calls ("scenario"); live session == RPC   │
                 └───────────────┬──────────────────────────────────────────────────────┘
                                 │  bench primitives (flash_*/reset_*/power/mux/harness/capture)
                 ┌───────────────▼──────────────────────────────────────────────────────┐
                 │  bench agents: Pi (SSH)   S31/P4 usbip bridge (HTTP+SCPI)   mux box   │
                 └──────────────────────────────────────────────────────────────────────┘
```

Concretely:

1. **Adopt hathach's tinyusb HIL harness as a supported interface, not just
   a source of ideas.** Its board JSON is the bench seat record, its
   `hil_flash.py` primitives (`flash_<name>` / `reset_<name>`, by-id
   resolution, probe pinning) are the bench primitive layer, its
   `hil_test.py` runner is a job script, and its `hil_report.json/.md` is a
   supported report output. Brent's "config file containing the board's
   port, hub port, how to reset, expected firmware name, flash offset" is
   that JSON plus our `power`, `aux`, `harness`, `capture` extensions. The
   controller imports and exports it. See §3.1, §3.7 and §3.8.
2. **Fix the requirements contract first** (audit items 1–3: one selection
   engine, explicit IDs still constrained, unknown fields rejected,
   `POST /v1/jobs/validate`). Add the `board`/`sensor` shorthand and
   `roles` from the follow-up design. Nothing new in this note contradicts
   that work; the session layer depends on it for aliases. See §3.2.
3. **Add a per-job session channel** (`/v1/jobs/{id}/session`, WebSocket,
   JSON-RPC) whose vocabulary mirrors the Wokwi wire protocol: `command` /
   `response` / `event`, every event stamped with time. Verbs: `serial.*`,
   `pin.*`, `control.set`, `power.*`, `mux.*`, `bus.*`, `capture.*`,
   `camera.*`, `firmware.flash`. Stages become **recorded sequences of these
   calls** (the Wokwi scenario-YAML idea), so a stage run and a live pytest
   session produce the same event log and assets. See §3.3.
4. **Ship two clients over that channel:** a `hil-client` Python package
   whose method names mirror `WokwiClient`, and a `pytest-hil` plugin whose
   `dut` fixture is API-compatible with pytest-embedded (`expect`,
   `expect_exact`, `write`, `dut[0]`/`dut[1]`). The plugin has two backends,
   `local` (Brent's Pi, no controller) and `controller`. Test bodies are
   identical in both. See §3.4.
5. **Model "controllable components" honestly.** Wokwi's `set_control(part,
   control, value)` maps onto real hardware in four ways (drive a pin,
   emulate an I2C/SPI target, route/power something, physically stimulate),
   and it does *not* map at all for most real sensors. The inventory gets a
   `controls` catalog per aux so a test can ask for "an accelerometer whose
   value I can set" and the planner can answer "emulated target on harness
   X" or "no". See §3.5.
6. **S31 bridge placement:** it is a bench agent with three faces (HTTP,
   MCP, SCPI harness on virtual busid `2-1`). Its virtual logic analyser is
   **P4-only**; for the S31 bench, captures come from a USB logic analyser
   exported over USB/IP and driven by sigrok on the controller, or from a
   P4 bridge. See §3.6.

The MVP cut for Brent is in §4; it is the `local` backend of item 4 built
on item 1, and it does not fork from the farm contract.

## 2. What the references actually contain

### 2.1 Brent's MVP to-do (9 October)

Bench: Feather ESP32 V2, Pi 5 orchestrator, RSHTECH RSH-ST10C-6 hub, a
STEMMA strand with TCA4307 buffer + BME680. Pipeline: pull CI artifact →
build a littlefs image from a local test `secrets.json` → merge → flash →
pytest (provisioning, connectivity, check-in) → report to GitHub Actions.
Tooling list: ProtoMQ, WipperSnapper_Protobuf, uhubctl, esptool,
mklittlefs. He wants a universal `flash_`/`reset_` script with only
`flash_esp`/`reset_esp` implemented, a board config file, pytest fixtures
(session, tests) and harnesses (serial monitor, flash, reset, make
filesystem, protomq), then `workflow_dispatch` to a HIL runner, then
fork-PR and nightly triggers.

Three things to flag to Brent (details in §4):

- The RSH-ST10C-6 is on uhubctl's list (`0BDA:0423`, 2024, "note" link).
  Verify it cuts VBUS and not just data; tinyusb learned the hard way that
  "off" on their hub is logical-only.
- The Feather ESP32 V2 has no native USB. Its by-id serial is the CP2102N
  bridge's serial, not the MCU's. Fine for the MVP; it is exactly the
  "bridge serial is not MCU identity" case PR #7 models.
- littlefs-merge is a stage our controller lacks (`write_secrets_msc` needs
  TinyUF2 MSC). It should be added controller-side as `make_fs_image` +
  `merge_firmware`, so the same job works on both benches.

### 2.2 tinyusb `test/hil/` (master @ `1a0e4020`, 2026-10-12)

Corrections to assumptions first: there is no Pi runner, no `pi4.json`, no
uf2/picotool flasher and **no hub/uhubctl fields in the board JSON**. The
`ci` rig is an x86 Proxmox VM with eight passed-through xHCI controllers;
uhubctl is only used by an out-of-band recovery script on the *root* port.

What is worth lifting:

- **Board JSON** (`hil_test.py` TypedDicts): `name`, `uid` (MCU unique ID the
  firmware reports as iSerialNumber), `tests {device,host,dual,only,skip,
  dev_attached[]}`, `flasher {name: jlink|stlink|openocd|esptool|lm4flash|
  pyocd, uid, args, vid_pid, verify}`, `flasher_recover`, `variant[]`,
  `logger: rtt`, `uart_uid`, `toolchain`, plus a `boards-skip` list.
- **Flasher dispatch**: `flash_primitive(name) = getattr(hil_flash,
  f"flash_{name}")`, `reset_<name>` optional (esptool has none),
  `FLASHER_SUFFIX` picks `.bin` vs `.elf`, `find_firmware()` → `skip (no
  binary)` rather than fail. `flash_esptool` reads `flash_args` /
  `config.env` from the build dir and uses the by-id port.
- **Device resolution without bus paths**: everything is `/dev/serial/by-id/
  usb-TinyUSB_TinyUSB_Device_<uid>-if00`, `/dev/disk/by-id/…`,
  `/dev/input/by-id/…`; `usb_scan()` reads sysfs lock-free fields first and
  is bounded so a wedged peer cannot stall it.
- **Isolation and recovery**: one forked process per board
  (`maxtasksperchild=1`), a kernel flock per board, `Reset_TT` on the hub
  port before flashing FS/LS DUTs, in-run recovery is a ~130 ms probe reset
  or a reflash, never a root-port VBUS cycle; `convoy_safe()` whitelists
  tools that pin a VID/PID before opening nodes.
- **Reporting**: `hil_report.json` → markdown table with ✅/❌/⚪ plus
  `board-wedged`, `pool-timeout`, `same-PID boundary` cells; a
  `<config>.failed` re-run spec consumed on `run_attempt != 1`; exit code =
  failure count; sticky PR comment via `workflow_run`.
- **CI flow**: build jobs upload `binaries-<toolchain>-*` artifacts;
  `hil-tinyusb` on `runs-on: [self-hosted, X64, hathach,
  hardware-in-the-loop]` downloads them with `merge-multiple`, runs
  `hil_test.py --retry 1`, uploads `hil-report-*`. PRs narrow the board set
  with `tools/change_impact.py`.

### 2.3 tannewt's CircuitPython HIL stack (branch head `c029ba19`, frozen since 2026-08-03)

The pytest branch has not moved and nothing hardware-related landed
upstream (`board_cache.py`, `esp.py`, `harness_client.py` 404 on
`adafruit/circuitpython` main). But Scott's stack is bigger than the branch:

| Repo | What it is | State |
|---|---|---|
| `tannewt/circuitpython@hardware-in-the-loop` | pytest: markers `circuitpython_board(id)`, `circuitpy_drive(files)`, `code_py_runs(n)`, `duration`; fixtures `circuitpython`, `circuitpython1/2` (BabbleSim multi-actor), session `harness`; `pytest_generate_tests` parametrises every test over `native_native_sim` + boards found by one mDNS scan; per-test FAT image flashed to the `user_fs` partition; pass = sentinel `Press any key to enter the REPL` then `assert … in serial.all_output` | 3 commits, single DUT per bridge, developer-machine only |
| `adafruit/esp-usbip-bridge` | ESP32-P4/S3 USB host → USB/IP on 3240, mDNS `_usbip._tcp` with `board_id` TXT | upstream of tyeth's S31 fork |
| `adafruit/esp-harness` | ESP32-P4 SCPI-1999 CDC harness: `:GPIO:DIR/PULL/WRITe/READ?/PULse`, `:BUS:I2C:CONT:*` (INIT/SCAN?/WRITe/READ?/XFER?), **`:BUS:I2C:TARG:*`** (INIT sda,scl,addr; WRITe preloads a byte queue the DUT will read; READ? pops what the DUT wrote), `:BUS:SPI:CONT/TARG:*`, `:BUS:UART:*`, `:DUT:PIN/WIRE/NAME` metadata in NVS; Python `Harness` (PyVISA) / `HarnessSerial` | last push 2026-07-10; target mode is a byte queue, no register map |
| `adafruit/esp-perfetto-logic` | P4 logic analyser: SUMP/OLS over CDC, 8 channels (array sized 16), up to 80 MHz, PSRAM depth, output format SUMP raw or streaming Perfetto trace, sigrok `ols` driver compatible | no README; built into the P4 bridge as virtual busid `2-1` |
| `adafruit/pyusb-usbip-backend`, `adafruit/pyserial-pyusb` | PyUSB backend that *is* a USB/IP client (no kernel vhci), `pyusb://host/vid:pid` serial URLs, pcapng URB capture, zeroconf discovery | PyPI `pyserial-pyusb` 0.1.0 |

Why this matters for us: `pyusb-usbip-backend` sidesteps the stale-vhci-port
problem (controller issue #5) and works from Windows/macOS developer
machines; esp-harness target mode is the only existing piece that gives a
Wokwi-like "set the value the DUT will read" on real I2C/SPI; the SUMP LA is
already a job-asset-shaped capture.

### 2.4 Wokwi client and CLI (`wokwi-client` 0.5.1, `@wokwi/cli` 0.28.1)

Wire protocol: JSON over WebSocket, server `hello` first, then
`{"type":"command","command","id","params"}` /
`{"type":"response","id","result","error"}` /
`{"type":"event","event","payload","nanos","paused"}`. Commands: `file:upload`,
`sim:start|pause|resume|restart|status|read-vcd`, `serial-monitor:listen|write`,
`pin:read|listen`, `gpio:list`, `control:set`, `touch:event`,
`framebuffer:read`, `coverage:read`, `pause-point:add|remove`, `gdb:*`.
Events: `serial-monitor:data`, `pin:change`, `chips:log`, `sim:pause`.

Python surface worth mirroring (`WokwiClient` / `WokwiClientSync`):
`connect`, `upload(name, bytes)`, `upload_file`, `start_simulation(firmware,
elf, pause, chips)`, `pause/resume/restart_simulation`,
`wait_until_simulation_time`, `serial_monitor(cb)`, `serial_monitor_cat`,
`serial_write`, `read_pin(part, pin)`, `listen_pin`, `gpio_list`,
`set_control(part, control, value)`, `touch_event`,
`read_framebuffer_png_bytes(id)`, `read_vcd()`. There is **no
`get_control`**; observation is pins, serial, framebuffer, VCD, chip logs.

Scenario YAML (`version: 1`, `steps:` one key per step): `delay`,
`wait-serial`, `write-serial`, `set-control {part-id, name, value}`,
`expect-pin {part-id, name, value}`, `take-screenshot {part-id, save-to |
compare-with}`, `touch*`, `expect-chip-output`. Per-part controls are
documented per part: `wokwi-pushbutton.pressed`, `wokwi-potentiometer
.position`, `wokwi-dht22.temperature/humidity`, `wokwi-ds18b20.temperature`,
`wokwi-mpu6050.accelX/Y/Z, rotationX/Y/Z, temperature`, `wokwi-hx711.load`,
`wokwi-photoresistor-sensor.lux`, joystick `x/y/pressed`.

`pytest-embedded-wokwi`: `--embedded-services idf,wokwi`, `wokwi` fixture
wraps `WokwiClientSync`, `dut` is a `WokwiDut` with the normal `expect`
API; the canonical test calls `wokwi.client.set_control('btn1','pressed',1)`
then `dut.expect('Button pressed 1 times')`.

The one thing that does not transfer: **pause points.** Wokwi's determinism
comes from stopping simulated time; real hardware cannot pause. Our
equivalent is monotonic timestamps on every event plus expect-with-timeout,
and the audit's rule that cross-instrument timestamps carry a declared
synchronisation uncertainty.

### 2.5 pytest-embedded and labgrid (for the fixture and lease vocabulary)

pytest-embedded: fixtures `dut`, `app`, `serial`, `pexpect_proc`,
`msg_queue`; `dut.expect`, `expect_exact`, `write`; `--embedded-services`
selects backends; `--count N` makes `dut`/`app` tuples with `|`-separated
per-DUT options; `--parallel-count/--parallel-index`; session flash cache;
teardown checks panics/core dumps.

labgrid: `--lg-env`, `--lg-coordinator`; fixtures `env`, `target`,
`strategy`; places with tags, `reserve --wait` → `LG_RESERVATION`, lock /
unlock; resources (`RawSerialPort`, `NetworkSerialPort`, …) bound to drivers
(`SerialDriver`, `ShellDriver`, …) and activated on demand; strategies with
`transition('shell')`; `@pytest.mark.lg_feature("camera")` skips when a
feature is absent. The audit's "allocation is a controller responsibility,
one authority" is labgrid's coordinator/exporter split.

### 2.6 esp-usbip-bridge, S31 branch (`a421cf9f`, 2026-10-08) and the mux box

HTTP (port 80, `GET /api/schema` enumerates everything): `/ping`,
`/api/info` (`hostname, board_id, target, version, uptime_s, free_heap,
auth_required, mux_bus_ok, usbip_port`), `/api/usb/devices[/{busid}]`
(404 = absent), `/api/usb/hubs` (`power_switching` ∈ `per-port|ganged|none|
unknown`), `/api/usb/enum_events` (per-port `verdict`/`diagnosis`),
`/api/ports[/{port}]`, `POST /api/ports/{port}/on|off {force}` (200, **202
pending** when the port is busy, 409 ganged/leads-to-hub without force),
`/cycle {off_ms, force}` (async), `/api/ports/off|on|restore`,
`/api/settings` (`enforce_per_port_switching`, `restore_port_power`,
`enum_timeout_ms`, overrides), `/api/auth/token`, `/api/reboot`, `POST /mcp`
(18 tools: `set_port_power`, `power_cycle_port`, `mux_select`, …). Mux API
is the CircuitPython box's verbatim (`/api/status`, `/api/select`,
`/api/isolate`, `/api/groups/{g}/select/{ch}`, `/api/groups/{g}/channel`,
`/api/topology`, `/api/probe`), so `adapters/analog_mux.py` targets either.

Virtual devices on bus 2: on the S31 profile only the **SCPI harness**
(`CAFE:4002`, busid `2-1`) with esp-harness's command set plus
`HUB:LIST?`, `HUB:PORT:POWer`, `HUB:PORT:CYCLe`, `USB:DEVices?`,
`MUX:SELect`, `MUX:ISOLate` (untested on hardware, issue #9). The
**Perfetto logic analyser is compiled only for `esp32p4`**
(`CONFIG_USBIP_VIRTUAL_LOGIC_ANALYZER` unset in
`sdkconfig.defaults.s31-function-coreboard-1`). No I2C sniffer, UART
passthrough, power monitor or sigrok/VCD export exists anywhere in the tree.
Bridge issue #19 (idle IN URBs starving OUT/control) is labelled fixed on
the branch (regression 20/20, serial open 9–16 ms); #22 (no TCP keepalive)
is open.

## 3. Proposed shape

### 3.1 Bench primitive layer: the board record

One JSON record per physical DUT seat, tinyusb-compatible where tinyusb has
a field, extended where our bench needs more. The controller's topology
importer and Brent's local scripts both read it.

```jsonc
{
  "name": "feather_esp32_v2",                 // tinyusb: build/board name
  "uid":  "CP2102N-6A1B2C3D",                 // tinyusb: serial used for /dev/serial/by-id
  "identity": {                               // ours (PR #7 twin vocabulary)
    "uid_owner": "usb-bridge",                // "mcu" | "usb-bridge" | "probe"
    "board_id": "adafruit:feather_esp32_v2",
    "arduino_fqbn": "esp32:esp32:adafruit_feather_esp32_v2",
    "circuitpython_board_id": null
  },
  "flasher": { "name": "esptool", "uid": "6A1B2C3D", "args": "--chip esp32 -b 460800",
               "vid_pid": "0x10c4 0xea60" },  // tinyusb shape, incl. vid_pid pin
  "reset":   { "method": "dtr-rts" },         // ours: dtr-rts | probe | power | bootsel
  "power":   { "kind": "uhubctl", "hub": "1-1", "port": 3, "vbus_verified": false },
            // or {"kind":"bridge-port","host":"usbip-d07a04","port":"1-1.3","force":true}
            // or {"kind":"solenoid","channel":5}
  "console": { "by_id": "usb-Silicon_Labs_CP2102N_…-if00-port0", "baud": 115200 },
  "firmware": { "artifact_glob": "*feather_esp32_v2*.bin", "offset": "0x0",
                "fs": { "kind": "littlefs", "partition": "spiffs", "secrets": "secrets.json" } },
  "aux": [ { "ref": "strand-brent-air", "route": {"mux": "mux-brent", "group": "muxA", "channel": 0} } ],
  "harness": { "ref": "harness-s31-2-1", "pins": { "BOOT": "T3", "RST": "T4", "SDA": "B1", "SCL": "B2" } },
  "capture": [ { "ref": "la-fx2-01", "channels": { "0": "SDA", "1": "SCL" } } ],
  "tests": { "skip": [], "only": [] }         // tinyusb
}
```

Rules carried over from tinyusb: resolve devices by by-id serial, never by
bus path at test time (bus paths stay in the inventory as *locators*,
which is also PR #7's position); pin the flasher by VID/PID where the tool
allows; `find_firmware()` missing → `skip`, not fail; one lock per seat.

Bench scripts (Python, importable by both backends):

| Module | Functions | Source |
|---|---|---|
| `bench/flash.py` | `flash_esptool`, `reset_esptool` (DTR/RTS), `flash_openocd`, `flash_uf2` (ours), `rescue_*` | lift from `hil_flash.py`; keep its `flash_<name>` dispatch |
| `bench/power.py` | `uhubctl_cycle(hub, port, off_ms)`, `bridge_cycle(host, port, force)`, `solenoid_cycle(ch)` | existing controller adapters + uhubctl |
| `bench/fs.py` | `make_littlefs(secrets_json, size)`, `merge(fw, fs, offset)` | Brent's new script; mklittlefs |
| `bench/serial.py` | by-id resolve, `SerialSaver`-style reader with `wait_for(text, timeout)` | tannewt `SerialSaver`; our `SerialCaptureAdapter.read_until` |
| `bench/harness.py` | `Harness` SCPI client (GPIO, I2C target preload, UART) | `esp-harness/host/harness.py` |
| `bench/capture.py` | `sump_capture(port, channels, rate, duration) -> perfetto|sr` | esp-perfetto-logic `host/capture.py`; sigrok-cli for USB LAs |

### 3.2 Requirements contract (unchanged from the audit, restated as the pytest target)

The audit's selector vocabulary stands: `device.{id|ids|kind|board_ids|
chip.{models,families,architectures}|capabilities|identifiers.*}`,
`requires[]` with `name`, `kind`, `components`, `capabilities`, `interface`,
`signals`, `rail`, `min_sample_rate_hz`; `pool`; shorthand
`board`/`sensor`; `roles{}` + `links[]`; `coverage`. Two additions this
note needs:

- `requires[].controls`: what the test must be able to *set* on that
  resource, e.g. `{"kind":"i2c_target","address":"0x68","controls":
  ["accelX","accelY","accelZ"]}` (see §3.5).
- `requires[].kind` gains `harness` (SCPI GPIO/bus harness attached to this
  DUT's named ports), `logic_analyzer`, `uart_adapter`, `power_monitor`
  as the audit proposed, with the attachment path resolved by the planner.

`POST /v1/jobs/validate?availability=ignore|current` returns the normalised
plan with `schema_valid / authorized / matchable / ready` and typed
rejections. The pytest plugin calls it at collection time (offline manifest
mode uses the published JSON Schema only).

### 3.3 Session channel: Wokwi shape on real hardware

`GET /v1/jobs/{id}/session` upgrades to WebSocket once the job is
`running` and the caller holds the lease token. Messages follow Wokwi's
envelope so the client code is a near-port:

```jsonc
// client → controller
{"type":"command","id":"7","command":"control.set",
 "params":{"binding":"accel","control":"accelZ","value":-1.0}}
// controller → client
{"type":"response","id":"7","result":{"applied":true,"via":"harness-s31-2-1:i2c_target"},"error":false}
// controller → client (unsolicited)
{"type":"event","event":"serial.data","payload":{"stream":"dut","bytes":"…"},
 "t":{"utc_ms":1760264000123,"mono_ns":8123456789,"source":"controller"}}
```

Command vocabulary (each one is also a stage type, see below):

| Group | Commands | Physical realisation |
|---|---|---|
| `serial` | `listen`, `write`, `expect {pattern, timeout_s}` | existing serial capture; `expect` is server-side so CI scripts and pytest share the matcher |
| `pin` | `read {binding, pin}`, `listen`, event `pin.change` | harness `:GPIO:READ?` polled, or an LA channel edge stream |
| `control` | `set {binding, control, value}` | harness GPIO/PULse, I2C/SPI target preload, DAC/digital-pot aux, mux channel, port power (see §3.5) |
| `power` | `on`, `off`, `cycle {off_ms}` | uhubctl / bridge port (202 pending honoured) / solenoid |
| `mux` | `select {strand|group,channel}`, `isolate` | ADG729 box or bridge |
| `bus` | `i2c.xfer {binding, addr, write, read_n}`, `i2c.scan` | harness controller mode or Pi i2c-dev; subject to the shared-bus ownership rule from the audit (solenoid hub on the same bus) |
| `capture` | `start {binding, channels, rate_hz, trigger}`, `stop`, `read {format: vcd|perfetto|sr}` | SUMP LA or sigrok-cli; result registered as a job asset with timebase metadata |
| `camera` | `snapshot {roi}` | existing `capture_display` |
| `firmware` | `flash {asset, offset}`, `enter_bootloader` | existing stages, re-usable inside a lease |
| `job` | `extend`, `release`, `mark {verdict…}` | existing |

Events: `serial.data`, `pin.change`, `power.state`, `mux.state`,
`usb.enumeration` (from the bridge's `enum_events` verdicts), `stage.begin|end`,
`capture.ready`, `log` (bench narration).

**Stages are recorded sessions.** `params.stages` keeps its one-object-per-
step shape; each existing stage is re-expressed as a sequence of the
commands above with the same verdict lines (`CHECKIN_VERDICT …`,
`I2C_PROBE_VERDICT …`), so `hil-lib.sh` and the skills keep working. New
generic stages mirror Wokwi's scenario steps directly: `set_control`,
`expect_serial`, `expect_pin`, `delay`, `write_serial`, `take_capture`.
Brent's littlefs path becomes `make_fs_image` + `merge_firmware` (or a
`firmware.fs` block that the controller applies before `flash`).

Why a WebSocket and not more REST: pin and serial events are streams, and
pytest's `expect` needs sub-second turnaround. Long-poll `/wait` remains for
CI scripts.

### 3.4 Clients

**`hil-client`** (Python, sync + async, one file per command group like
`wokwi_client`): `HilClient(token, server)`, `client.validate(req)`,
`job = client.submit(req)`, `job.wait()`, `async with job.session() as s:`
then `s.serial_monitor(cb)`, `s.serial_write`, `s.expect(pattern,
timeout)`, `s.read_pin(binding, pin)`, `s.listen_pin`, `s.set_control(
binding, control, value)`, `s.power_cycle`, `s.mux_select`,
`s.capture(...) -> Asset`, `s.snapshot(roi) -> bytes`. Method names follow
Wokwi's where the semantics match so people who have used Wokwi CI read it
without a glossary.

**`pytest-hil`** plugin:

- Markers compile to the requirements schema (rules per the follow-up
  design). `@pytest.mark.hil(board="feather_esp32_v2")`,
  `@pytest.mark.hil(board="pyportal", sensor="scd4x")`,
  `@pytest.mark.hil(scenario="…", coverage="each_board")`,
  `@pytest.mark.hil_feature("camera")` (labgrid's `lg_feature` semantics:
  skip with reason, never silently narrow).
- Fixtures: `hil` (allocation + session handle), `dut` (pytest-embedded
  compatible: `expect`, `expect_exact`, `write`, `serial`; tuple for
  `roles` or `--count`), `harness`, `strand`, `capture`, `camera`,
  `firmware` (resolved artifact + profile).
- `--hil-backend=local|controller`. `local` reads the board JSON from §3.1,
  uses `bench/*` directly, and takes a per-seat flock (tinyusb). `controller`
  uses `hil-client`. Same test bodies.
- `--hil-manifest` exports requirements + coverage at collection without
  touching hardware; `--hil-reserve` (labgrid `reserve --wait`) blocks until
  a cohort is allocated and exports `HIL_LEASE` for `xdist` workers.
- JUnit classification: `invalid_requirements`, `incompatible`,
  `unavailable`, `infra_error`, `dut_failure` as distinct outcomes, mirroring
  tinyusb's `board-wedged`/`pool-timeout` cells.

**CI**: keep `examples/hil-call.sh`; add a composite action with
`validate → submit → wait → assets → junit`, a sticky PR comment rendered
from the same report JSON (tinyusb `pr_comment.yml` pattern), and a
`.failed` re-run spec on `run_attempt != 1`.

### 3.5 "Wokwi-like" component control on real hardware

Wokwi's triple `(part, control, value)` becomes `(binding, control, value)`
where `binding` is a `requires[].name`. The inventory declares, per aux or
harness, which controls exist and how they are realised:

```jsonc
{ "id": "harness-s31-2-1", "kind": "harness", "model": "esp-harness",
  "controls": [
    {"name": "BOOT",   "type": "gpio",       "values": [0,1],       "via": ":GPIO:WRITe"},
    {"name": "RST",    "type": "pulse",      "unit": "us",          "via": ":GPIO:PULse"},
    {"name": "i2c_target", "type": "i2c_target", "address_range": ["0x08","0x77"],
     "mode": "byte-queue", "via": ":BUS:I2C:TARG:WRITe"}
  ],
  "observables": [ {"name": "pin", "type": "gpio", "via": ":GPIO:READ?"} ] }
```

Four realisations, in order of how much of Wokwi's catalogue they cover:

| Wokwi control | Real-hardware realisation | Available now? |
|---|---|---|
| `pushbutton.pressed`, slide switch, DUT `BOOT`/`RST` | harness GPIO drives the DUT pin (`:GPIO:WRITe`, `:GPIO:PULse`) | yes on P4/S31 harness `2-1` (SCPI untested on S31 hardware) |
| `potentiometer.position`, LDR `lux`, NTC, joystick | analog stimulus: DAC or digital pot aux on the DUT's ADC pin (MCP4725 / DS3502 — DS3502 is proven on the Jumperless bench; Jumperless V5 DACs + crossbar give both wiring and level) | needs a stimulus aux; harness has no DAC |
| `dht22.temperature`, `mpu6050.accelX…`, `ds18b20.temperature` | **target emulation**: harness I2C/SPI target preloads the bytes the DUT will read. esp-harness is a byte queue, so a per-sensor *emulator profile* (register map → byte stream for the DUT's expected read sequence) lives in the controller. Register-map mode is the obvious upstream contribution to esp-harness. Alternative: a companion CircuitPython board running `i2ctarget` | partly: works for simple fixed-read sensors; register-pointer sensors need the profile |
| Any real sensor (BME680, SCD4x, INA260) | cannot be *set*. Two honest patterns: (a) **reference reader**: the Pi or harness reads the same sensor over the strand before/after the DUT and the test asserts agreement within tolerance; (b) **physical stimulus** rig: heater, LED, servo tilt (±1 g on an accelerometer at 0°/90°), declared as a `controls` entry on the rig aux | (a) needs the shared-bus ownership rule; (b) per-rig |

Rules: a `control.set` on a `requires[]` binding is only accepted when the
inventory lists that control; the response says `via` so the evidence log
shows whether the value was emulated, driven, or physically applied; a test
that marks `sensor="mpu6050"` without `controls` gets a real part if one
exists, and a test that marks `controls=["accelZ"]` only matches an
emulator binding. Those are two different requirements and the planner must
not blur them (the audit's "co2 is not eCO2" rule applied to control).

Jumperless V5 is the long-tail answer to Wokwi's `connections[]`: a
programmable crossbar plus DACs/ADCs/GPIO. Its known caveats (hardware I2C
only through the crossbar, 200 ms settle, raw-REPL fragility) make it a
phase-3 aux, not an MVP dependency.

### 3.6 Where the S31 bridge and captures fit

- The bridge is a **bench agent without a shell** (PR #4 wording). Its
  three faces map onto the session verbs: HTTP for `power.*`/`mux.*`/
  `usb.enumeration`, SCPI on `2-1` for `pin.*`/`control.set`/`bus.*`, MCP
  for operators.
- Honour the bridge's **202 `pending`** on port power: `power.cycle` must
  await the `enum_events` verdict (`ok`/`rebooting`) rather than a fixed
  delay; this is the same detection-driven rule `power_cycle` already uses
  for solenoids.
- **Logic capture on the S31 bench**: no on-bridge LA. Options, in
  preference order: (1) a USB logic analyser (fx2lafw-class or Saleae
  clone) on a bridge port, attached over USB/IP to the controller, driven by
  `sigrok-cli`, registered as `kind: logic_analyzer` with the bridge port as
  its locator; (2) a P4 bridge (`p4hil` profile) where `2-1` is the Perfetto
  LA and `2-2` the harness; (3) the P4 LA as a standalone USB device on an
  S31 port. All three produce the same `capture.read` asset
  (`vcd|perfetto|sr`) with `timebase: {source, rate_hz, sync: "unsynchronised"}`.
- **Developer path without vhci**: `pyusb-usbip-backend` + `pyserial-pyusb`
  let a developer run `pytest --hil-backend=local` against a bridge from
  Windows/macOS with no kernel module and no stale-port state. Worth a spike
  before building anything else on vhci.
- Identity: busid `1-1.3` is a locator; `uid` comes from the device
  descriptor or the MCU (PR #7). A bridge DUT seat in the §3.1 record is
  `power.kind = bridge-port` and `console.by_id` is the vhci tty's by-id on
  the controller.

### 3.7 Debug probes and non-esptool flashers (tinyusb learnings, for the Pico bench)

The requirements follow-up adds Pico W / Pico 2 W seats with PicoProbes, and
the audit proposes `requires: [{kind: "debug_probe", interface: "swd",
capabilities: ["halt", "memory-read"]}]`. tinyusb has run SWD/JTAG probes
in CI for years; the rules below come from its code and commit history
(`hil_flash.py`, `hil_test.py`, `usbtest.py` @ `1a0e4020`).

- **Probe identity is the flasher record, not the DUT's.** `flasher.uid` is
  the probe serial (J-Link S/N, ST-Link `sn=`, CMSIS-DAP `adapter serial
  <uid>`, pyocd `-u cmsisdap:<uid>`); `flasher.vid_pid` pins openocd/pyocd
  to one USB node (`-c "adapter usb vid_pid 0x2e8a 0x000c"`). A probe seat in
  the §3.1 record therefore has its own `uid`, and a PicoProbe is
  `{"name":"openocd","uid":"<cmsis-dap serial>","args":"-f interface/
  cmsis-dap.cfg -f target/rp2040.cfg","vid_pid":"0x2e8a 0x000c"}`
  (`rp2350.cfg` for Pico 2 W). This is the audit's "probe belongs to its
  physical attachment, not a board-name match" rule in practice.
- **Pin before you open.** openocd only *warns* on a malformed VID/PID pin
  and then opens every USB node; a D-state node on a wedged peer makes that
  hang unkillably. tinyusb validates the pin with a regex and keeps a
  `convoy_safe()` whitelist of tools that filter before opening (esptool,
  STM32_Programmer_CLI, pyocd with vid_pid, openocd pinned or on
  `interface/jlink.cfg|stlink.cfg|ti-icdi.cfg`, `dfu-util -d vid:pid -S
  <uid>`). JLinkExe cannot be pinned, so every J-Link board also carries a
  `flasher_recover` openocd route to the same probe. Our bench agent should
  enforce the same whitelist before any probe command.
- **Recovery is a probe reset, never a port cycle, during a run.**
  `reset_openocd`/`reset_jlink`/`reset_stlink`/`reset_pyocd` take about
  130 ms; esptool has no reset primitive, so its recovery is a reflash.
  Root-port VBUS cycling is an out-of-band operator action
  (`usb_recover.sh root-cycle`). For RP2040/RP2350 a wedged DAP is recovered
  through the **Rescue DP** (`set RESCUE 1` / `rp2350-rescue.cfg`) only when
  the openocd output contains `DAP_WEDGED` strings. Map these onto the
  session verbs as `power.reset {via: probe}` versus `power.cycle {via:
  port}` so a test can ask for the cheap one and the planner can refuse it
  when no probe is attached.
- **Verify defaults are per tool.** openocd verifies unless
  `"verify": false` (WCH parts never read back correctly); ST-Link verify
  is opt-in; esptool verifies as a separate stage. Keep `flasher.verify`
  as a tri-state in the record.
- **Console without a VCOM.** `logger: "rtt"` routes the console through
  J-Link RTT; `uart_uid` names a separate UART bridge when the probe has no
  VCOM. PicoProbe does have a VCOM, so a Pico seat's `console.by_id` is the
  probe's `-if01`, which again means the console serial is the probe's
  identity, not the Pico's.
- **Firmware selection follows the flasher.** `FLASHER_SUFFIX` picks `.bin`
  for esptool/lm4flash and `.elf` for probes; `find_firmware()` returning
  nothing yields a `skip (no binary)` cell. Our `firmware` fixture should
  do the same per `flasher.name` rather than assuming `.bin`, and a Pico
  seat additionally supports UF2 via BOOTSEL power-cycling, which stays
  `flash_uf2` with `reset.method = "bootsel"`.

What tinyusb does *not* give us: debugger sessions as a test resource
(halt, breakpoints, memory read). It uses probes only to flash and reset.
Those operations stay as the audit's `capabilities` on `debug_probe`, backed
by an openocd GDB server the controller launches inside the lease and
exposes through the session (`debug.attach`, `debug.halt`, `debug.read
{addr, n}`), which is phase 3 work.

### 3.8 tinyusb HIL as a supported interface

Four surfaces, each a thin adapter over what already exists in tinyusb
`test/hil/` @ `1a0e4020`:

| Surface | What the farm does | Where it lands |
|---|---|---|
| **Inventory in** | `hil topology import --tinyusb boards.json`: every `boards[]` entry becomes a seat with `flasher`, `uid`, `tests`, `dev_attached`, `variant`, `logger`, `uart_uid` preserved verbatim; `boards-skip` seeds `status: unavailable, kind: permanent`. Our fields (`power`, `aux`, `harness`, `capture`, `identity`) are additive and ignored by stock `hil_test.py`. | topology seeder; PR #7 twins record `uid` with its owner |
| **Inventory out** | `GET /v1/targets?format=tinyusb` (and `hil targets --tinyusb > local.json`) emits a valid tinyusb config for the seats the caller may use, or for the seats in a lease. This is exactly tinyusb's "minimal rig: boards on any PC + `test/hil/local.json`" path, so tinyusb's own CI could point `hil_test.py` at our bench as a second rig. | `api/targets.py` |
| **Runner as a job** | `script: "tinyusb-hil"`, `params: {config: <seat subset>, build_dir, board_tests: {"<board>": [...]}, retry: 1, accumulate: false}`. The controller leases the seats, materialises `local.json` for them, stages the `cmake-build` artifact tree, runs `hil_test.py --retry 1 -b <seat> …` on the seat's bench host (Pi over SSH, or the controller over USB/IP for bridge seats), and registers `hil_report.json`, `hil_report.md`, `<config>.failed` and the audio/ directory as job assets. Pass/fail = exit code (= failure count), with `board-wedged`/`pool-timeout` cells mapped to the `infra_error` outcome rather than `dut_failure`. | `adapters/tinyusb_hil.py`; `worker.py` script dispatch |
| **Report out** | `hil_report.json` is the canonical per-seat result table for *every* script, not only tinyusb's: `firmware-bench` and `pytest-hil` runs render the same cells (✅/❌/⚪, `board-wedged`, `pool-timeout`, `same-PID boundary`, `board-locked`) so one sticky PR comment renderer (tinyusb's `pr_comment.yml` pattern) serves all three. | `hil_report` helper vendored; CI composite action |

Vendoring: `test/hil/` is not a package (no `pyproject`, `helper/` modules
are imported relative to the script). Pin it as a git submodule at
`vendor/tinyusb-hil` (sparse checkout of `test/hil`) and import
`hil_flash`, `helper.hil_util`, `helper.hil_report`, `helper.hil_lock` from
there, with `bench/flash.py` re-exporting the `flash_<name>` primitives and
adding ours (`flash_uf2`, `flash_tinyuf2_msc`, `reset_bootsel`,
`reset_solenoid`). Propose upstream a `pyproject.toml` under `test/hil`
(entry points `hil_test`, `hil_flash`) so the submodule becomes a pip
dependency; that is a small, welcome PR and keeps us on hathach's fixes
(the Oct 2026 commits `d229dfdb`, `dbc77592`, `b0d3e6ce`, `5e4ba3d4` all
touched the parts we would use).

Constraints the adapter must respect so `hil_test.py` behaves: one seat
per forked worker (`maxtasksperchild=1`) and the per-board flock remain
tinyusb's; the controller's lease is the outer lock, the flock the inner;
`HIL_REPORT_DIR` is set per job; `convoy_safe()`'s whitelist is enforced for
any probe command the controller issues outside `hil_test.py`; root-port
VBUS cycling is never issued inside a run. `dev_attached` fixtures (the USB
peripherals a host-capable DUT enumerates) are a new aux kind
`usb_fixture` with `vid_pid` + `serial`, so the `host/*` tests can be
requested through `requires` like any other resource.

## 4. Brent's MVP, mapped so it does not fork

| Brent's subtask | Do it as | Reuse |
|---|---|---|
| Bench setup | One §3.1 record for the Feather V2 seat; verify the RSH-ST10C-6 cuts VBUS (`uhubctl -l <hub> -p <port> -a cycle -d 2`, then watch the by-id node vanish). Record `vbus_verified`. | uhubctl README hub list (`0BDA:0423`) |
| Build/flash infra | `bench/flash.py` (`flash_esptool`, `reset_esptool` via DTR/RTS) + `bench/fs.py` (`make_littlefs`, `merge`) + `bench/power.py`. Config = the board record, not a new format. | tinyusb `hil_flash.py`, esptool `merge_bin`, mklittlefs |
| Local pytest on Pi | `pytest-hil --hil-backend=local`: `dut` fixture (expect/write), `firmware` fixture (artifact+fs merge, session flash cache like pytest-embedded), `protomq` fixture (existing WS-Python harness), `power` fixture. Tests: `test_provisioning` (fs present, secrets valid: read back via serial or MSC where available), `test_connectivity` (Wi-Fi join), `test_checkin` (broker registration; the controller's `verify_checkin` matcher ported as a function). | pytest-embedded fixture names; tannewt sentinel-wait pattern |
| Preliminary GH automation | tinyusb's shape: build workflow uploads `binaries-*`; `ws-hil.yml` on `runs-on: [self-hosted, hil, feather_esp32_v2]` downloads, runs `pytest --junitxml`, uploads logs; `workflow_run` sticky comment. | tinyusb `build.yml` / `pr_comment.yml` |
| Fork PRs / nightly | Fork PRs: `pull_request_target` + label gate (tinyusb gates on `repository_owner`). Nightly: `schedule`. Same job. | — |
| "LLM confirms the Pi sees the board" | The bridge/MCP pattern already exists (`list_usb_devices`); on a Pi, expose the same via a tiny MCP wrapper over `bench/*` or just `lsusb`/by-id in a skill. | esp-usbip-bridge MCP |

The only controller work the MVP needs *eventually* is `make_fs_image` /
`merge_firmware` so the identical job runs through the farm. Brent's Pi can
be registered as an SSH host in our topology later with no change to his
tests, because the `controller` backend consumes the same board record.

## 5. Decisions: adopt / adapt / reject

| Source | Adopt | Adapt | Reject |
|---|---|---|---|
| tinyusb | **the whole `test/hil/` harness as a supported interface** (§3.8): board JSON as seat record (import + export), `hil_flash.py` primitives vendored, `hil_test.py` as the `tinyusb-hil` job script, `hil_report.json/.md` as the common report; VID/PID pinning, `flasher_recover`, Rescue DP, `.failed` re-run, `--retry 1`, per-seat flock, "never root-port cycle in-run" | sysfs scan bounds → our presence probe; `dev_attached` → aux kind `usb_fixture`; lease outer / flock inner | multiprocessing pool as the farm scheduler (it stays inside one job); x86-only runner assumption |
| tannewt | per-test FS image to a partition (generalises secrets provisioning); sentinel wait + `all_output` assert; mDNS `_usbip._tcp` discovery with `board_id`; esp-harness SCPI; SUMP→Perfetto captures; pyusb USB/IP backend | marker names (`circuitpython_board` → `hil(board=…)`) | one-board-per-host cache; session-wide flash of every cached board |
| Wokwi | command/response/event envelope; `set_control` triple; scenario step vocabulary; `read_pin`/`listen_pin`; framebuffer→camera | time: `nanos` → `{utc_ms, mono_ns, source}`; `wait_until` → expect-with-timeout | pause points; `sim:restart` semantics |
| pytest-embedded | `dut.expect/expect_exact/write`; `--count` tuples; `--embedded-services` → `--hil-backend`; junit; session flash cache | — | ESP-IDF-only firmware resolver |
| labgrid | reservations (`reserve --wait`, env token); `lg_feature` skip semantics; resource→driver binding vocabulary; one coordinator owns allocation | places/tags → our `pool` + selectors | its YAML env as the user-facing contract |
| esp-usbip-bridge | HTTP/MCP/SCPI as the no-shell host; 202 pending + enum verdicts; mux API parity | LA via USB/IP + sigrok on S31 | expecting an on-bridge LA on S31 |

## 6. Sequence

1. **Now (Brent, local backend):** vendor tinyusb `test/hil` at `1a0e4020`,
   write the Feather V2 seat as a tinyusb-schema record (§3.1), `bench/*`
   re-exporting `hil_flash` primitives, `pytest-hil --hil-backend=local`,
   `ws-hil.yml`. Target: provisioning/connectivity/check-in green on the
   Feather V2 from a `workflow_dispatch`, reported as `hil_report.md`.
2. **Controller contract (audit 1–3):** one selection engine, ID + constraints,
   unknown-field rejection, `jobs/validate`, `make_fs_image`/`merge_firmware`
   stages; tinyusb import/export of the inventory and the `tinyusb-hil` job
   script (§3.8); merge PR #4, then PR #7.
3. **Session channel + `hil-client` + `controller` backend:** serial/pin/
   control/power/mux/capture verbs; stages re-expressed as recorded sessions;
   `requires` kinds `harness`, `logic_analyzer` with attachment paths; USB LA
   over USB/IP + sigrok on the S31 bench; `pyusb-usbip-backend` spike.
4. **Shorthand, roles, coverage, controls catalog:** `board`/`sensor`
   shorthand, `roles`/`links`, atomic cohort leases, `controls` with I2C
   target emulator profiles (and a register-map proposal to esp-harness),
   reference-reader sensor pattern, physical stimulus rigs; Jumperless as a
   wiring/stimulus aux.

## 7. Open questions for the user and Brent

- Where does the MVP code live: a new `adafruit/WipperSnapper_HIL` (Brent's
  bench + `pytest-hil` + `bench/*`), or `examples/` here with the plugin
  published from this repo? The board record and plugin must have one home.
- Allocation authority in CI: does `pytest-hil --hil-backend=controller` run
  on the GitHub runner (allocating via the API) or does the controller run
  pytest on a bench host (the current `pytest-suite` path)? Both fit; pick
  one default so leases have one owner.
- Is a P4 bridge for the capture-heavy bench acceptable, or should the S31
  bench get a USB logic analyser first? The S31 cannot host the Perfetto LA.
- esp-harness target mode is a byte queue. Do we build sensor emulator
  profiles controller-side, or propose a register-map mode upstream first?
- Brent's hub: confirm VBUS switching on the RSH-ST10C-6 before anything
  depends on a cold boot.
