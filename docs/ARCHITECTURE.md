# USB-IP HIL Controller — Architecture

Status: **Draft v0.1** — design only, no code yet. This document is the
contract the first implementation will be built against. Comments and
revisions welcome before we start cutting code.

**Related docs:** [`hil-regression-pipeline.md`](hil-regression-pipeline.md) —
the end-to-end firmware A/B regression pipeline (CI → Tailscale → controller →
bench → DUT; the pixelWrite/#927 case). [`device-availability.md`](device-availability.md)
— target availability + self-rectification (`GET /v1/targets`).

## 1. Goal

Provide a small HTTP service, hosted on a Raspberry Pi, that lets a
GitHub Actions job (or any CI runner) request a hardware-in-the-loop
test against a real device and receive structured pass/fail + logs back,
**without** the calling repository having to run a self-hosted GitHub
runner.

The service is the seam between:

- **Caller side** — a CI step that submits a job and waits for a result
  (long-poll, up to ~5 min per call, re-issued on timeout).
- **Controller host** — runs *this* repository. An independent host
  (any Linux box with network reachability to the bench), **not** one
  of the HIL hosts itself. Owns the API, the dashboard, the SQLite
  job store, and the scheduler. Does not have any DUTs attached to
  it.
- **HIL host fleet** — a set of bench Pis the controller fans work
  out to over SSH. Each HIL host owns a slice of the DUTs:
  - `rpi-displays` (Pi Zero 2W, `192.168.1.234`) — **all microcontroller
    DUTs**, attached via the Genesys USB hub with MCP23017 solenoid
    power/reset control at I²C `0x20`.
  - `rpi-hil001` … `rpi-hil007` (one per host) — **single-board-computer
    DUTs**, currently on a separate USB hub without per-port power
    control. Independent port-power control is planned for parity with
    rpi-displays.
  - SSH access on every HIL host is via key-based auth to the `pi`
    user; the controller holds the private key.
- **ProtoMQ broker host** — `pi5-protomq` (`192.168.1.210`, MQTT
  `1884`). DUTs talk to it as MQTT clients during protomq-flavoured
  tests; the controller observes the broker but doesn't host it.

A thin web UI sits in front of the same API for human inspection of
queue state, device availability, the wiring graph (devices + aux +
mux), and per-job logs / artifacts. The same topology endpoints are
available to CI so a job can ask "is there a seat for me?" before
submitting, instead of finding out by failing.

## 2. Non-goals (v1)

- Multi-Pi clustering. Single host, single in-process worker pool.
- Dynamic hardware switching (programmable matrix / crossbar). Devices
  are fixed to ports; cameras observe known positions. Planned later.
- Running arbitrary user-supplied shell scripts. v1 only runs a
  pre-registered allow-list of test scripts, parameterised by job input.
- Hosting GitHub-managed self-hosted runners. We are deliberately
  avoiding that cost model.
- Authenticated browser sessions for the public dashboard. Read-only
  pages are public on the LAN; write operations require an API token.

## 3. Top-level shape

```
                +--------------------------------------+
   GitHub CI ── │   Controller host (this repo)        │ ── HTMX dashboard
   (POST job,   │   FastAPI app (single process)       │    (same origin)
    long-poll)  │                                      │
                │  ┌──────────┐   ┌──────────────┐    │
                │  │  HTTP /  │   │ Scheduler +  │    │
                │  │  HTMX    │──▶│ worker pool  │────┼────────────────────┐
                │  │  router  │   │ (asyncio)    │    │  per-host SSH      │
                │  └────┬─────┘   └──────┬───────┘    │  transport         │
                │       │                │            │                    │
                │       ▼                ▼            │                    │
                │   ┌─────────────────────────┐      │                    │
                │   │  SQLite (jobs, events,  │      │                    │
                │   │  devices, hosts, tokens,│      │                    │
                │   │  audit)                 │      │                    │
                │   └─────────────────────────┘      │                    │
                +--------------------------------------+                    │
                                                                            │
                                  ┌─────────────────────────────────────────┘
                                  │
                                  ▼
                       HIL host fleet (each owns its DUTs)
                       ┌──────────────────────────────────────────────┐
                       │ rpi-displays — microcontroller DUTs          │
                       │   Genesys USB hub + MCP23017 solenoid (0x20) │
                       │   /dev/serial/by-id/..., esptool, picotool   │
                       │   vendor/usbip-autoattach (server side)      │
                       ├──────────────────────────────────────────────┤
                       │ rpi-hil001 … rpi-hil007 — SBC DUTs           │
                       │   (per-port power control planned)           │
                       └──────────────────────────────────────────────┘

                       ProtoMQ broker host (read-only observer)
                       ┌──────────────────────────────────────────────┐
                       │ pi5-protomq — MQTT 1884, web UI 5173         │
                       └──────────────────────────────────────────────┘
```

One controller process, one SQLite file, async workers in the same
event loop. No Redis, no Celery, no external broker in v1. Each
worker holds an SSH session to the HIL host that owns its assigned
device; the work happens *there*, not on the controller.

## 4. Repository layout (proposed)

```
docs/
  ARCHITECTURE.md          # this file
  API.md                   # to follow once endpoints firm up
  DEPLOY.md                # systemd unit, user creation, udev rules
vendor/                    # git submodules — see vendor/README.md
  protomq/                 # tyeth-ai-assisted/protomq @ displays-v2-testing
                           #   (dual-push to tyeth/protomq)
  usbip-autoattach/        # tyeth-ai-assisted/usbip-autoattach @ main
  hil-detection/           # tyeth-ai-assisted/hil-detection @ main
  wippersnapper-arduino/   # tyeth-ai-assisted/adafruit-Adafruit_Wippersnapper_Arduino @ migrate-api-v2
                           #   (fetch-only upstream remote to adafruit/...)
                           # Python WS variant is private/unreleased — not vendored;
                           # wiring info for SBC targets comes from vendor/protomq/scripts.
scripts/
  setup-submodules.sh      # dual-push for vendor/protomq, upstream remote for WS Arduino
examples/                  # caller-side templates a downstream repo copies into its CI
  wippersnapper-arduino/secrets.example.json
  wippersnapper-python/{secrets.example.json,.env.example}
  hil-call.sh              # submit + long-poll reference flow
  README.md
.github/workflows/
  example-hil-call.yml     # workflow_dispatch example of the caller flow
src/
  hil_controller/
    __init__.py
    main.py                # FastAPI app factory + uvicorn entry
    config.py              # pydantic-settings, env-driven
    db/
      models.py            # SQLAlchemy models
      migrations/          # alembic
    api/
      jobs.py              # POST /v1/jobs, GET /v1/jobs/{id}, /wait, /logs
      devices.py           # GET /v1/devices…
      health.py
    web/
      templates/           # Jinja2 + HTMX partials
      static/
      routes.py            # dashboard pages
    auth/
      tokens.py            # per-repo bearer tokens
      oidc.py              # GitHub Actions OIDC verifier
      policy.py            # repo → allowed device-pool mapping
    queue/
      scheduler.py         # in-process scheduler, per-device locks
      worker.py            # job runner state machine
      events.py            # append-only log + long-poll wakeups
    hosts/
      base.py              # HostTransport protocol (exec, copy_to, copy_from, stream)
      ssh.py               # default transport: asyncssh-based, key auth
      agent.py             # future: HTTP agent transport (see §15 OQ11)
      registry.py          # host pool from YAML, health checks, key paths
    adapters/
      base.py              # DeviceAdapter protocol — calls into the HostTransport
      usbip.py             # invokes vendor/usbip-autoattach on the remote host
      solenoid_hub.py      # invokes vendor/hil-detection/usb_hub.py on the remote host
      flashers/            # esptool, picotool, dfu, uf2-msc, snapper-py, …
      serial_capture.py    # streams the remote /dev/serial/by-id/... over SSH
      camera.py            # optional frame grabs alongside serial
    topology/
      manifest.py          # YAML loader for devices + aux + mux matrix
      resolver.py          # selector → (device, aux bindings, mux ops)
      importers/
        protomq_scripts.py # parse vendor/protomq/scripts/*.json → manifest
        hardware_md.py     # parse vendor/hil-detection/references/hardware.md
    tests/
      runner.py            # invokes named scripts from the HIL repo
      protomq.py           # ProtoMQ-specific log-assertion helpers
tests/                     # pytest, unit + integration with fakes
deploy/
  systemd/
  udev/
  README.md
pyproject.toml
```

This layout exists to be argued with — we should land it before writing
code so import paths don't churn.

## 5. Domain model

### 5.1 Host

A HIL host the controller can fan work out to. Modelled as a
first-class entity because devices, auxes, muxes, and per-job
adapter calls are all scoped to one. The controller itself does *not*
have a Host row — it's the orchestrator, not a worker.

| Field             | Notes                                                       |
|-------------------|-------------------------------------------------------------|
| `id`                    | Stable slug, e.g. `rpi-displays`, `rpi-hil003`.       |
| `role`                  | `microcontroller-fleet` / `sbc-fleet` / `protomq-broker`. |
| `addr`                  | Hostname or IP.                                       |
| `transport`             | `ssh` (v1 default) or `agent` (see §15 OQ11).         |
| `ssh_user`              | Default `pi`.                                         |
| `ssh_key_path`          | Path on the controller filesystem to the per-host key. |
| `capabilities`          | Tags the resolver can require, e.g. `power-control`,  |
|                         | `usbip-server`, `mcp23017`, `cameras`.                |
| `max_concurrent_jobs`   | Hard cap on simultaneous jobs on this host. Default: |
|                         | `1` for `sbc-fleet` (one SBC HIL host runs one test  |
|                         | or suite at a time — the bench can't usefully share   |
|                         | a Pi between two CI jobs), unbounded (`null`) for     |
|                         | `microcontroller-fleet` (per-device locks are the     |
|                         | real constraint there). See §9 for how exclusive-host |
|                         | jobs interact with this. |
| `status`                | `available` / `quarantined` / `offline`.              |
| `last_seen_at`          | Updated by periodic health probe.                     |

Host rows are seeded from `/etc/hil/hosts.yaml`. Periodic health
probes (SSH `true` over the configured key) update `status` and
`last_seen_at` so the dashboard can show offline hosts before a job
is wasted.

### 5.2 Device

A physical thing wired to a HIL host — the *unit under test*.

| Field             | Notes                                                       |
|-------------------|-------------------------------------------------------------|
| `id`              | Short stable slug, e.g. `qtpy-s3-01`, `rpi-hil003-pi5b`.    |
| `host_id`         | FK to the Host that owns this device.                       |
| `kind`            | `microcontroller` / `sbc` / `snapper-arduino` / …           |
| `model`           | More specific tag (`rp2040`, `esp32-s3`, `pi-zero-2w`, …).  |
| `capabilities`    | Tags the resolver matches on (`spi`, `i2c`, `cdc-acm`, …).  |
| `usb_path`        | Stable `by-path` or USB-IP busid (on the owning host).      |
| `reset`           | `{ mux, channel, profile }` — nullable if device self-resets. |
| `flasher`         | Name of registered flasher adapter.                         |
| `serial_port`     | `/dev/serial/by-id/...` *on the host*, baud, parity.        |
| `camera_id`       | Optional v4l2 device on the host watching this position.    |
| `pool`            | Logical grouping for authorization (`public`, `internal`, …).|
| `exposable_via`   | `local`, `usbip`, or both. Controls remote-attach jobs.     |
| `status`          | `available` / `in-use` / `quarantined` / `offline`.         |

A device on its own is not usually enough to run a meaningful test —
you also need the right peripherals wired to it. Those live in
§5.5 (auxiliary components) and §5.6 (connectivity matrix).

Devices are seeded from a YAML file checked into `deploy/`. The DB row
is hydrated on startup; humans can mark a device `quarantined` from the
dashboard without editing YAML.

### 5.3 Job

| Field             | Notes                                                       |
|-------------------|-------------------------------------------------------------|
| `id`              | UUIDv7, sortable.                                           |
| `submitted_by`    | Token id or OIDC subject (repo+workflow+ref).               |
| `repo`            | `owner/name` from auth context, not request body.           |
| `request`         | JSON: target selector, script name, params, payload, secrets_profile, flags. |
| `secrets_profile` | Resolved profile id (see §5.8), pinned at submission time so a hot-reload of the policy file doesn't change what a queued job sees. |
| `exclusive_host`  | Bool. If true, the job takes an exclusive lock on its assigned host: no other job may run there, and the worker can attribute every `dmesg` / `usbmon` / serial line unambiguously. Trade-off: blocks other jobs on the same host for the duration, so reserve it for hard-to-trace problems. |
| `state`           | See state machine below.                                    |
| `assigned_host`   | Filled in at `assigned` transition.                         |
| `assigned_device` | Filled in at `assigned` transition.                         |
| `created_at`, `started_at`, `finished_at`                                       |
| `result`          | `pass` / `fail` / `error` / `timeout` / `cancelled`.        |
| `summary`         | Short human string for dashboard rows.                      |
| `artifacts`       | List of saved files: serial log, camera frames, exit codes, dmesg/usbmon (when exclusive_host=true). |

### 5.4 Job event log

Append-only. One row per state transition or worker-emitted line.
Powers (a) the long-poll wakeup, (b) the live HTMX log view, (c) audit.

| Field        | Notes                                                       |
|--------------|-------------------------------------------------------------|
| `job_id`     | FK.                                                         |
| `seq`        | Monotonic per job, assigned at insert.                      |
| `at`         | Timestamp.                                                  |
| `kind`       | `state` / `log` / `metric` / `artifact`.                    |
| `payload`    | JSON.                                                       |

Long-poll clients pass a `since=<seq>` cursor and block on a
per-job `asyncio.Condition` until new rows land or the timeout fires.

### 5.5 Auxiliary component

An auxiliary is anything that hangs off a device and that a test might
need to assert against — a display, a sensor, an LED ring, a logic
analyser channel, a power monitor, a second microcontroller acting as a
companion. Modelled separately from devices because (a) the same
physical aux can serve more than one device via a multiplexer, and (b)
test scripts care about *capabilities*, not which port a thing happens
to be on this week.

| Field             | Notes                                                       |
|-------------------|-------------------------------------------------------------|
| `id`              | Stable slug, e.g. `ili9341-2.4-01`.                         |
| `kind`            | `display` / `sensor` / `actuator` / `companion-mcu` / …     |
| `model`           | Specific part, e.g. `ili9341`, `ssd1306`, `bme280`.         |
| `capabilities`    | Tags the resolver matches on (`display:240x320`, `i2c-target:0x76`). |
| `interface`       | `spi` / `i2c` / `uart` / `gpio` / `usb-cdc`.                |
| `signals`         | Logical names → bus pin/line (`mosi`, `cs`, `sda`, `int`).  |
| `observability`   | How the controller can read it back: `camera`, `serial-tap`, `i2c-sniffer`, or `none`. |
| `pool`            | Same authorization model as devices.                        |
| `status`          | `available` / `in-use` / `faulty` / `offline`.              |

Aux records are seeded from the same YAML manifest as devices. The
manifest is also the place where physical wiring gets recorded —
today that knowledge lives implicitly inside the protomq display-v2
test scripts (see §15 open question 7).

### 5.6 Connectivity matrix (multiplex)

Most aux-to-device wiring in v1 is **fixed**: aux `X` is bolted to
device `Y` and only `Y`. But the bench *does* have a real shared
resource: the Genesys Logic USB hub (`05e3:0610`, dual-cascaded on
`rpi-displays`) with an **MCP23017 solenoid driver at I²C `0x20`**
controlling power/reset per channel. That hub is effectively a
per-port power mux; the manifest models it uniformly so the scheduler
can answer "can I give job J a device of kind K with a 240x320 SPI
display attached?" without the caller knowing which physical seats
satisfy that. Future genuine signal muxes (TCA9548 I²C switch, SPI
mux, relay GPIO crossbar) drop into the same model.

```yaml
# /etc/hil/topology.yaml  (excerpt — grounded in vendor/hil-detection/references/hardware.md)
hosts:
  - id: rpi-displays
    role: microcontroller-fleet
    addr: 192.168.1.234
    transport: ssh
    ssh_user: pi
    ssh_key_path: /etc/hil/keys/rpi-displays
    capabilities: [power-control, mcp23017, usbip-server, cameras]
  - id: rpi-hil001
    role: sbc-fleet
    addr: rpi-hil001.local
    transport: ssh
    ssh_user: pi
    ssh_key_path: /etc/hil/keys/rpi-hil-fleet      # shared key, per-host known_hosts pinning
    capabilities: []                                # power-control coming later
  # rpi-hil002 .. rpi-hil007 follow the same shape.
  - id: pi5-protomq
    role: protomq-broker
    addr: 192.168.1.210
    transport: none                                 # read-only observer; no SSH
    mqtt_port: 1884

muxes:
  - id: rpi-displays.usb-hub-01
    host: rpi-displays
    kind: usb-power
    model: genesys-logic-05e3-0610
    control:
      adapter: mcp23017-solenoid
      i2c_bus: 1
      i2c_addr: 0x20
      script: vendor/hil-detection/usb_hub.py       # path on the HIL host after deploy
    exclusive: false                                # per-channel independent
    timing_profiles:
      standard:   { on: "200ms-H,LOW", off: "200ms-H,500ms-L,1000ms-H,LOW" }
      samd51_uf2: { on: "200ms-H,LOW", off: "200ms-H,100ms-L,300ms-H,LOW" }
  # Future: rpi-hil001.usb-hub-01 etc. once per-port power control lands.

devices:
  # Sample bench mapping only. Live solenoid assignments drift over time;
  # consult vendor/hil-detection/references/hardware.md before trusting a
  # destructive flash sequence. On 2026-06-12 the live bench retest put the
  # QT Py ESP32-S3 validation target on channel 4, not channel 0.
  - id: qtpy-s3-01
    host_id: rpi-displays
    kind: microcontroller
    model: esp32-s3
    capabilities: [native-cdc, spi, i2c]
    usb: { vid: "239a", pid: "8143" }
    serial_port: /dev/serial/by-id/...
    reset: { mux: rpi-displays.usb-hub-01, channel: 0, profile: standard }
    pool: public
  - id: pyportal-titano-01
    host_id: rpi-displays
    kind: microcontroller
    model: samd51
    capabilities: [uf2, spi, i2c]
    usb: { vid: "239a", pid: "8053", uf2_vid: "239a", uf2_pid: "0035" }
    flasher: uf2-msc
    reset: { mux: rpi-displays.usb-hub-01, channel: 4, profile: samd51_uf2 }
    pool: public
  - id: pico-w-01
    host_id: rpi-displays
    kind: microcontroller
    model: rp2040
    capabilities: [bootsel-msc, spi, i2c]
    flasher: picotool
    reset: { mux: rpi-displays.usb-hub-01, channel: 6, profile: standard }
    pool: public
  - id: rpi-hil003-pi5-a
    host_id: rpi-hil003
    kind: sbc
    model: pi5
    capabilities: [linux, python-snapper]
    serial_port: /dev/serial/by-id/...              # UART via host
    reset: null                                     # no power control yet — manual or via systemd reboot over SSH
    pool: internal

auxes:
  - id: oled128x32-metro-s2
    kind: display
    model: ssd1306
    interface: i2c
    capabilities: [display, "display:128x32", "display:i2c"]
    signals: { sda: D47, scl: D48, addr: 0x3c }
    observability: camera
  - id: qualia-round-480-01
    kind: display
    model: st7701
    interface: ttl-rgb666
    capabilities: [display, "display:480x480", "display:ttl-rgb666"]
    signals: { r: [D1,D2,D42], g: [D21,D47,D48], b: [D10,D11,D12] }
    observability: camera

connections:
  # Fixed wiring: imported from vendor/protomq/scripts/*.json.
  - aux: oled128x32-metro-s2
    device: metro-s2-01
    bus: i2c0
  - aux: qualia-round-480-01
    device: qualia-s3-01
    bus: ttl-rgb666
```

Three resolver rules fall out of this:

1. **Fixed-wired aux** is implicitly locked when its device is locked.
   This is the v1 common case.
2. **Multiplexed aux** is a separate lockable resource; the scheduler
   acquires `(device, aux, mux)` together, then issues a mux switch
   adapter call before the worker enters `preparing`.
3. **Exclusive muxes** serialise across all downstream devices for the
   duration of any job that uses *any* aux on that mux. The resolver
   reports this so the dashboard can show why a device is blocked even
   though it looks idle.

If no mux exists, jobs that need an aux the assigned device can't
reach are rejected at submission time with a structured error rather
than failing mid-flash.

### 5.7 Where the manifest comes from

Two existing artefacts seed the first `topology.yaml` — neither is
authoritative on its own, but together they cover the bench:

- **`vendor/hil-detection/references/hardware.md`** — hosts, USB hub
  VID:PID, MCP23017 control address, the 8-channel solenoid map
  (channel → board, USB VID:PID, USB serial, expected `/dev/ttyACM*`),
  and the timing profiles. Source of truth for *power/reset wiring*.
- **`vendor/protomq/scripts/*.json`** — one demo per `(board, display)`
  pair. Each file has `display.add` with one of `i2c { busSda, busScl,
  deviceAddress }`, `spi { … }`, or `ttlRgb666 { pinR0…pinB2 }`, the
  driver name (`SSD1306`, `SH1107`, `ST7701`, …), and the panel ID.
  Source of truth for *device↔display wiring + display interface*.

The `protomq_scripts.py` importer:

1. Walks `vendor/protomq/scripts/*.json`.
2. Parses the filename (`feather-s2-sh1107-demo.json` →
   `device=feather-s2`, `aux=sh1107`) and the `display.add` step.
3. Emits an `auxes:` entry per unique `(driver, panel, interface)`
   tuple and a `connections:` entry binding it to the device.
4. Cross-references the device against `hardware.md` to fill in
   `usb.vid/pid`, `reset.channel`, and `reset.profile`.
5. Writes a draft `topology.yaml` and a `topology.unresolved.md`
   listing any (a) scripts whose device can't be found in
   `hardware.md`, (b) `hardware.md` channels with no matching script,
   (c) pin assignments that vary between scripts targeting the same
   board.

Re-running the importer is non-destructive — humans own the final
`topology.yaml`; the importer regenerates a side-by-side `*.imported`
file for diffing.

### 5.8 Secret profile

A *secret profile* is the named bundle of credentials the controller
materialises onto a HIL host at deploy/flash time. Profiles let us
target the bench ProtoMQ broker, an Adafruit IO test account, or the
live Adafruit IO production endpoint with the same job-submission
body — the caller picks a profile name (or the policy picks one for
them) and the controller substitutes the right values into the
rendered `secrets.json` / `.env`.

```yaml
# /etc/hil/secrets-profiles.yaml  (illustrative)
profiles:
  - id: bench-protomq           # default; bench ProtoMQ broker, throw-away test AIO account
    io_url:  pi5-protomq.local
    io_port: 1884
    io_username: ${env:HIL_BENCH_AIO_USERNAME}
    io_key:      ${env:HIL_BENCH_AIO_KEY}
    wifi_ssid:     ${env:HIL_BENCH_WIFI_SSID}
    wifi_password: ${env:HIL_BENCH_WIFI_PASSWORD}

  - id: live-io-test            # live io.adafruit.com, a dedicated test account
    io_url:  io.adafruit.com
    io_port: 8883
    io_username: ${env:HIL_LIVE_AIO_TEST_USERNAME}
    io_key:      ${env:HIL_LIVE_AIO_TEST_KEY}
    wifi_ssid:     ${env:HIL_BENCH_WIFI_SSID}
    wifi_password: ${env:HIL_BENCH_WIFI_PASSWORD}

  - id: live-io-prod            # live io.adafruit.com, production account — guarded
    io_url:  io.adafruit.com
    io_port: 8883
    io_username: ${env:HIL_LIVE_AIO_PROD_USERNAME}
    io_key:      ${env:HIL_LIVE_AIO_PROD_KEY}
    wifi_ssid:     ${env:HIL_BENCH_WIFI_SSID}
    wifi_password: ${env:HIL_BENCH_WIFI_PASSWORD}
    requires_trusted: true       # only callers with the `trusted-firmware` policy bit
```

| Field              | Notes                                                       |
|--------------------|-------------------------------------------------------------|
| `id`               | Stable slug. Pinned on the Job row at submission.           |
| `io_url`, `io_port`| MQTT broker the firmware will talk to.                      |
| `io_username`, `io_key` | AIO credentials. Stored only as `${env:...}` references — never plaintext in the YAML. The controller reads the actual values from systemd `EnvironmentFile=` or a sealed `/etc/hil/secrets.env` at startup. |
| `wifi_ssid`, `wifi_password` | Network creds for the DUT to reach the broker.    |
| `extra`            | Optional dict of additional key/values for arbitrary firmware-specific secrets the test happens to need. |
| `requires_trusted` | Bool. If true, the auth policy must grant the caller the `trusted-firmware` capability to use this profile. |

Policy (§8.1, §8.2) maps a principal to a list of allowed profile
ids and, optionally, a default. Jobs submitted without an explicit
`secrets_profile` get the principal's default; jobs requesting a
profile outside the allow-list are rejected at the API boundary.

Materialisation flow (controller-side, per job):

1. Load the resolved profile values into memory.
2. Render the relevant `examples/.../secrets.example.json` (or `.env`)
   with `envsubst` into the job's per-host scratch directory.
3. `copy_to` the rendered file to `/tmp/hil/<job-id>/secrets.json`
   on the HIL host, mode `0400`, owner `pi`.
4. Worker invokes the flash/deploy adapter, which references the
   secrets path.
5. At job terminal (success, failure, or cancel), the worker
   `rm -f`s the secrets file before releasing the device. SQLite
   records that the file was wiped but **never the contents**.

The controller never echoes a profile's secret values back through
the API — `/v1/jobs/{id}` shows only the profile id used.

## 6. Job state machine

```
  queued ──▶ assigned ──▶ preparing ──▶ flashing ──▶ running ──▶ finished
     │           │             │            │            │
     │           ▼             ▼            ▼            ▼
     └───── cancelled      error         error        timeout
```

- `queued`     — accepted by API, awaiting a free device that matches.
- `assigned`   — scheduler picked a device, took its lock.
- `preparing`  — USB-IP attach / power-on / pre-flight checks.
- `flashing`   — writing the requested artifact, if any.
- `running`    — test script executing; serial capture + assertions live.
- `finished`   — terminal, with `result` populated.
- `error`      — infrastructure failure (flash failed, USB-IP gone, etc).
                 Distinct from `finished` + `fail`, which is "device ran
                 the test and the test said no".
- `timeout`    — wall-clock or per-phase budget exceeded.
- `cancelled`  — operator action via dashboard or API.

Transitions write a `state` event; the worker also writes `log` events
inline so callers see progress, not just terminal status.

## 7. HTTP surface (v1)

All API routes under `/v1`. JSON in/out. Bearer auth required for
writes. Dashboard at `/`.

| Method | Path                              | Auth     | Purpose                                  |
|--------|-----------------------------------|----------|------------------------------------------|
| POST   | `/v1/jobs`                        | required | Submit job. Returns `{id, wait_url}`.    |
| GET    | `/v1/jobs/{id}`                   | required | Current snapshot (state, result, etc).   |
| GET    | `/v1/jobs/{id}/wait?since=&timeout=` | required | Long-poll. Blocks up to `timeout` (cap 600s) or until new events. Returns events + current state. CI re-issues on timeout. |
| GET    | `/v1/jobs/{id}/logs?since=`       | required | Same as wait but non-blocking; for tools that prefer polling. |
| GET    | `/v1/jobs/{id}/artifacts/{name}`  | required | Stream a captured artifact (serial log, frame, etc). |
| POST   | `/v1/jobs/{id}/cancel`            | required | Best-effort cancel.                      |
| GET    | `/v1/hosts`                       | required | List HIL hosts, with `status`, `last_seen_at`, and the device count owned by each. |
| GET    | `/v1/hosts/{id}`                  | required | Host detail + devices on it + recent jobs run there. |
| GET    | `/v1/devices`                     | required | List devices visible to the caller's pool. Supports `?host=&kind=&model=&capability=&aux=&pool=` filters; `?include=aux,connections,host` to expand. |
| GET    | `/v1/devices/{id}`                | required | Device detail + current job + attached/reachable aux + owning host. |
| GET    | `/v1/aux`                         | required | List auxiliary components. Same filter/include grammar. |
| GET    | `/v1/aux/{id}`                    | required | Aux detail + which devices it can be routed to (and via which mux). |
| GET    | `/v1/topology`                    | required | Full graph: hosts, devices, auxes, muxes, connections. Suitable for the dashboard's wiring view and for CI to introspect before submission. |
| POST   | `/v1/topology/resolve`            | required | Dry-run a job selector → returns matching `(host, device, aux bindings, mux ops)` candidates and any structured rejection reason. No job is created. |
| GET    | `/healthz`, `/readyz`             | none     | Liveness / readiness.                    |
| GET    | `/`, `/jobs`, `/jobs/{id}`, `/hosts`, `/devices`, `/topology` | none (read-only) | HTMX dashboard pages. |

### 7.1 `POST /v1/jobs` body

Microcontroller job — firmware binary + named test script:

```json
{
  "target": {
    "device": { "kind": "microcontroller", "model": "rp2040" },
    "requires": [
      { "kind": "display", "capabilities": ["display:240x320", "display:spi"] },
      { "kind": "sensor",  "model": "bme280" }
    ],
    "pool": "public"
  },
  "script": "protomq.validate-logs",
  "params": { "scenario": "boot-handshake" },
  "payload": {
    "kind": "firmware-binary",
    "source": {
      "kind": "github-release",                  // or "github-actions-artifact", "url+sha256"
      "repo": "owner/name",
      "tag": "v1.2.3",
      "asset": "firmware.bin",
      "sha256": "…"
    }
  },
  "secrets_profile": "bench-protomq",
  "exclusive": { "host": false },
  "timeouts": { "total_s": 600, "flash_s": 120, "run_s": 300 },
  "metadata": { "pr": 42, "commit": "abc1234" }
}
```

SBC job — git clone + ref + entry-point script:

```json
{
  "target": {
    "device": { "kind": "sbc", "model": "pi5" },
    "pool": "internal"
  },
  "script": "git-clone-and-run",                 // allow-listed entry-point runner
  "params": { "entry": "tests/run_hil.py", "args": ["--smoke"] },
  "payload": {
    "kind": "git-source",
    "source": {
      "repo":   "https://github.com/owner/name.git",   // https or ssh
      "ref":    "abc1234",                              // sha, tag, or branch
      "submodules": true,
      "shallow": true,
      "setup":  ["pip", "install", "-e", "."]           // optional, run after checkout
    }
  },
  "secrets_profile": "live-io-test",
  "exclusive": { "host": true },                  // SBC hosts already max-1, but flag carries through
  "timeouts": { "total_s": 1800, "deploy_s": 300, "run_s": 1200 }
}
```

Key fields:

- **`target.device`** — concrete `{ "id": "rp2040-01" }` or abstract
  selector (`kind` / `model` / `capabilities`); the topology
  resolver picks the least-loaded matching seat.
- **`target.requires`** — auxiliary selectors that must be physically
  attached **or reachable via a mux**. Resolver runs *before* the
  job is accepted, so callers get a structured 409 with the
  unsatisfiable selector rather than a mid-flash failure.
- **`payload.kind`** — `firmware-binary` (MCU path, downloaded by
  hash) or `git-source` (SBC path, cloned + checked out on the HIL
  host). Optional only for scripts that exercise pre-provisioned
  hardware (USB-IP attach to an already-flashed device, etc).
- **`secrets_profile`** — opt-in identifier into §5.8. Omit to use
  the principal's default profile. Mismatch with the auth policy →
  403 at submit time.
- **`exclusive.host`** — when true, the assigned host runs *only*
  this job for the duration, dmesg/usbmon capture starts, and the
  job artifacts grow a `host-dmesg.log` and `host-usbmon.pcap` (see
  §9 for the scheduler interaction and §10 for what gets captured).
  SBC hosts are already max-1, so the flag is a no-op there; on
  rpi-displays it actually changes behaviour.

CI that wants to see what's available before submitting can call
`POST /v1/topology/resolve` with the same `target` block and get
back the candidate seats without enqueueing anything.

### 7.2 Long-poll semantics

CI flow:

```
POST /v1/jobs                       -> 202 {id, wait_url, since: 0}
GET  /v1/jobs/{id}/wait?since=0     -> 200 {events:[…], next_since: 17, state: "running"}
GET  /v1/jobs/{id}/wait?since=17    -> 200 {events:[],   next_since: 17, state: "running"}  (timeout, re-poll)
GET  /v1/jobs/{id}/wait?since=17    -> 200 {events:[…], next_since: 31, state: "finished", result: "pass"}
```

Server-side cap on `timeout` is 300s by default, 600s hard ceiling, to
keep within reverse-proxy and GitHub-side network budgets. The client
just re-issues — there is no special "still alive" response.

## 8. Auth

Two paths, both producing the same internal `Principal` object that
downstream policy checks against.

### 8.1 Per-repo / per-client bearer tokens

- Generated from the dashboard by an admin (admin auth out of scope for
  v1 — assume LAN-trusted operator). Stored as `argon2` hash; the plain
  token shown once at creation.
- Token row carries: `id`, `label`, `repo` (optional pin), `pool`
  (which device pool it can target), `created_at`, `revoked_at`,
  `last_used_at`.
- Presented as `Authorization: Bearer hil_<id>_<secret>`. The `id`
  prefix lets us look up the row without a full-table scan.
- Suitable for non-GitHub callers (a developer's laptop, a Jenkins job,
  a cron).

**Future: token management web UI (not yet implemented)**

- Superusers (any authenticated user in the initial rollout) should be
  able to list, create, and revoke tokens from `/ui/tokens` without
  SSHing into the host and running `mint-token.py`.
- Each "user" concept is just a label grouping multiple token rows; a
  person can hold several tokens (e.g. a dev-laptop token and a CI
  token with different pool/profile scopes).
- The create flow: fill label + pools + profiles, server calls
  `mint-token.py` logic internally, shows the plain token once, stores
  only the hash.
- Revoke: sets `revoked_at`; the token is rejected on next use.
- Implementation note: requires a `/v1/tokens` CRUD API (POST, GET,
  DELETE) guarded by a `manage-tokens` capability, and a
  `/ui/tokens` page mirroring the hosts/devices admin pattern.

### 8.2 GitHub Actions OIDC

- CI step requests an OIDC token from GitHub
  (`id-token: write` permission) and presents it as a bearer.
- Server verifies: signature against
  `https://token.actions.githubusercontent.com/.well-known/jwks` (JWKS
  cached with TTL), `iss`, `aud` (we set our own audience and document
  it), `exp`/`iat`.
- Claim → `Principal` mapping uses `repository`, `repository_owner`,
  `ref`, `workflow`, `job_workflow_ref`, `environment`.
- Policy file (YAML, hot-reloaded) maps claims to device pools and
  secret profiles (§5.8), and optionally grants capabilities. E.g.:

  ```yaml
  - match: { repository: "adafruit/protomq", ref: "refs/heads/main" }
    allow_pools:    ["public", "protomq"]
    allow_profiles: ["bench-protomq", "live-io-test"]
    default_profile: bench-protomq
    capabilities:   []
  - match: { repository: "adafruit/wippersnapper-arduino-firmware" }
    allow_pools:    ["public", "protomq", "wippersnapper"]
    allow_profiles: ["bench-protomq", "live-io-test", "live-io-prod"]
    default_profile: live-io-test
    capabilities:   ["trusted-firmware"]   # may run raw-firmware-smoke
  - match: { repository_owner: "adafruit" }
    allow_pools:    ["public"]
    allow_profiles: ["bench-protomq"]
    default_profile: bench-protomq
  ```

- No long-lived secret on the CI side; revocation is by editing the
  policy file.
- Org membership gating (Adafruit / adafruitinternaldev): GitHub OIDC
  tokens carry `repository_owner` as a claim, so gating on
  `repository_owner: "adafruit"` or `"adafruitinternaldev"` requires no
  GitHub API call — the claim is in the signed JWT. An allow-list of
  `repository_owner` values (plus optionally specific repos) in the
  policy YAML is the recommended approach.  No OAuth app or PAT needed.

Both paths produce a `Principal { kind, subject, repo, allowed_pools,
allowed_profiles, default_profile, capabilities }` which the job
submission handler uses to (a) reject jobs targeting disallowed
pools or profiles, (b) gate access to permissive scripts behind the
`trusted-firmware` capability, (c) stamp `repo` and `submitted_by`
on the row from the auth context, never from the request body.

## 9. Scheduler & worker

- One `asyncio` scheduler task. Wakes on (a) new job enqueued,
  (b) device freed, (c) host status change, (d) periodic tick.
- Per-device `asyncio.Lock`. A job in `assigned`+ holds its device's
  lock until terminal.
- **Per-host concurrency**: each Host row carries
  `max_concurrent_jobs` (§5.1). The scheduler models it as a
  per-host `asyncio.Semaphore`. SBC hosts (`rpi-hil00N`) default to
  1 — a SBC HIL host runs one test or suite at a time, period.
  Microcontroller hosts (`rpi-displays`) default to unbounded; per-
  device locks are the real constraint there.
- **Per-host exclusive lock** (for `exclusive_host: true` jobs):
  separate from the semaphore, a per-host `asyncio.Lock` that is
  acquired *write-style*. Semantics:
  - A job requesting `exclusive.host = true` must wait until every
    other in-flight job on that host has reached terminal.
  - Once an exclusive job is `queued` for a host, the scheduler
    refuses to *start* further non-exclusive jobs on that host
    (existing in-flight ones drain). This prevents an exclusive job
    from being starved by a steady drip of regular jobs.
  - While an exclusive job is running, the worker turns on dmesg
    capture (`journalctl -k -f`) and usbmon (`cat /sys/kernel/debug/
    usb/usbmon/0u` or `tcpdump -i usbmonX -w …`) on the HIL host;
    artifacts grow `host-dmesg.log` and `host-usbmon.pcap`. These
    captures are unambiguous because no other job is touching the
    USB bus on that host for the duration.
- **Routing at assignment**: the resolver picks a `(host, device,
  aux bindings, mux ops)` tuple — taking host semaphore availability
  and the exclusive-pending state into account. The worker opens a
  transport session against `device.host_id` and runs every adapter
  call through it; nothing about the work itself happens on the
  controller. Hosts in `offline` or `quarantined` status are
  filtered out of resolution.
- Worker per active job (`asyncio.create_task`). Worker drives the
  state machine, emits events, calls into the device adapter (which
  in turn goes through the host transport).
- Graceful shutdown: scheduler stops accepting new starts, in-flight
  workers get a `cancel()` budget which propagates as SSH session
  close on the HIL host, then process exits. SQLite state is the
  source of truth; on restart, any non-terminal job is marked
  `error` with reason `restart` (we do not auto-resume hardware work
  on a possibly-orphaned remote process).

## 10. Hardware adapter layer

Two cooperating abstractions: a **host transport** that gives the
worker a way to run commands and stream data on a remote HIL host,
and **device adapters** that compose host-transport calls into
device-shaped operations (acquire / reset / flash / serial /
release). The worker only sees the adapter; the adapter is the only
thing that knows the transport exists.

### 10.1 Host transport

```python
class HostTransport(Protocol):
    async def exec(self, argv: list[str], *, env: dict[str, str] | None = None,
                   stdin: bytes | None = None) -> ExecResult: ...
    async def stream(self, argv: list[str]) -> AsyncIterator[bytes]: ...   # stdout
    async def copy_to(self, local: Path, remote: PurePosixPath) -> None: ...
    async def copy_from(self, remote: PurePosixPath, local: Path) -> None: ...
    async def open_serial(self, device_node: str, baud: int) -> AsyncIterator[bytes]: ...
    async def healthcheck(self) -> bool: ...
```

V1 default implementation is **SSH** (`asyncssh`), key-based auth,
known_hosts pinned per HIL host. The transport pools one persistent
connection per host and multiplexes channels over it so individual
adapter calls don't pay connection setup latency. Serial streaming
goes through an SSH channel running `socat OPEN:/dev/serial/by-id/...
- ` (or equivalent) so the bytestream is line-buffered to the
controller without temp files.

**`LocalTransport`** (`hosts/local.py`) is a second concrete
implementation that runs commands via `asyncio.create_subprocess_exec`
on the controller machine itself. Topology hosts with `kind: local`
route to it; no SSH config needed. Used for localhost SBC jobs (e.g.
running Wippersnapper_Python pytest suites without real SBC hardware)
and for CI environments where the controller and test target are the
same machine.

A future `agent` transport — small Python service running as a
systemd unit on each HIL host, exposing an HTTPS API the controller
calls — is sketched as a drop-in alternative once SSH's failure modes
become painful (process supervision, partial stdout on connection
drop, sandboxing). See §15 OQ11.

### 10.2 Device adapters

```python
class DeviceAdapter(Protocol):
    async def acquire(self) -> None: ...           # power on, usbip attach, etc
    async def reset(self) -> None: ...             # solenoid pulse or DTR toggle
    async def flash(self, artifact: Artifact) -> None: ...
    async def open_serial(self) -> AsyncIterator[bytes]: ...
    async def release(self) -> None: ...           # detach, power off
```

Each adapter holds a `HostTransport` for the device's owning host.
Concrete adapters compose smaller pieces, *all* of which run on the
HIL host:

- `UsbIpAttach` — uses `vendor/usbip-autoattach`. That repo is split:
  the **server side** (udev rule + `usbip-autobind` helper) is
  installed on the HIL host that physically owns the USB device, so
  the device stays bound to `usbip-host` across resets; the **client
  side** (stdlib-only Python reconciliation loop) runs on whichever
  HIL host is consuming the USB-IP-exported device — the controller
  supervises that loop over SSH.
- `Mcp23017Solenoid` — invokes `vendor/hil-detection/usb_hub.py` on
  `rpi-displays` (the only host with a solenoid-controlled hub
  today). Channel per-device from the manifest; **timing profile**
  (`standard`, `samd51_uf2`, …) also from the manifest, because
  SAMD51 boards need a specific short/long pulse sequence to enter
  the UF2 bootloader vs the standard off sequence. When `rpi-hil00N`
  gain per-port power control, each grows its own mux record and the
  same adapter is parameterised by host.
- `Flasher` — pluggable, all invoked through the host transport so
  the firmware artifact is copied to the HIL host's `/tmp` first and
  the flash tool runs *there*: `esptool` (ESP), `picotool` +
  1200-baud CDC sentinel (RP2040 BOOTSEL chain, see
  `vendor/hil-detection/scripts/pico_hil_flash.sh` for the three-
  stage strategy already in use), `uf2-msc` (mount the BOOTSEL drive
  and copy `.uf2`), an Arduino-sketch upload, or "no-op" for
  pre-provisioned devices. Selected via `payload.kind ==
  "firmware-binary"` plus the device's `flasher` field.
- `GitDeploy` — the SBC-side counterpart of the flashers. Triggered
  by `payload.kind == "git-source"`. Steps, all over the host
  transport, all on the SBC HIL host's filesystem:
  1. `git clone --depth=1 --branch=<ref>` (or full clone + checkout
     if `shallow=false`); `--recurse-submodules` if requested.
  2. Render the chosen secrets profile (§5.8) into the workspace —
     `secrets.json` for the long-running Python-Wippersnapper client,
     `.env` for pytest runs.
  3. Optional `payload.source.setup` argv list (typically
     `pip install -e .` or `npm ci`).
  4. Hand off to the test runner (§11) with the workspace path and
     the chosen `params.entry` script.
  5. On terminal, wipe the secrets file (§5.8 step 5) and remove the
     workspace.
  Note: a Python-Wippersnapper *installer* (deploy the long-lived
  client onto an SBC and start it as a service) is a small wrapper
  around `GitDeploy` that runs a systemd-unit install step after
  setup; it isn't a separate Flasher.
- `SerialCapture` — opens a streaming SSH channel against the host's
  `/dev/serial/by-id/...` (stable ID, because `ttyACM*` numbering is
  unstable across re-enumeration). Line-buffered, tee'd to both the
  event log and an on-disk artifact file on the controller.
- `CameraCapture` — captures frames on the HIL host (where the
  camera is attached), pulls them back over the transport's
  `copy_from` for storage in the per-job artifact directory.

Test scripts (section 11) call into the same adapter the worker
holds, so a script can request an additional reset or a fresh serial
window mid-test without knowing the transport details.

## 11. Test scripts (the "HIL repo")

Scripts are not arbitrary code submitted at request time. They are
named, versioned entry points provided either by:

- The `hil-testing` repo, vendored or installed as a Python package.
- This repo's `src/hil_controller/tests/` for built-ins (ProtoMQ
  helpers, generic "boot and look for string", etc).

A script signature:

```python
REQUIRES = TopologySpec(
    device={"kind": "microcontroller", "capabilities": ["spi"]},
    aux=[
        {"kind": "display", "capabilities": ["display:240x320", "display:spi"]},
    ],
)

async def run(ctx: TestContext, params: dict) -> TestResult: ...
```

Each script declares a `REQUIRES` topology spec. The dashboard's
script catalogue and `GET /v1/topology/resolve` both use it so a
caller can ask "which seats can run `protomq.display-smoke` right
now?" without reading the script source.

`TestContext` exposes the adapter handle, the bound aux handles
(`ctx.aux["display"]`), a `log()` helper that goes to the event
stream, and helpers like `expect_serial("READY", timeout=10)` and
`assert_log_contains(pattern)` for the ProtoMQ case.

Allow-list of script names lives in config; submissions referencing an
unknown name — or a known name whose `REQUIRES` cannot be satisfied by
any seat in the caller's pool — are rejected at the API boundary.

Two **permissive built-ins** exist for the "trusted user wants to
test something unknown" case, both gated by the `trusted-firmware`
policy capability (§8 / §5.8 `requires_trusted`):

- `raw-firmware-smoke` — for the MCU path. Flashes the
  `payload.firmware-binary`, resets the device, opens serial, and
  streams whatever comes out for `params.observe_s` seconds. The
  script doesn't assert anything itself; the job result is `pass`
  unless the flasher itself failed or the device never enumerated
  back. Useful for "does this firmware boot at all" smoke tests
  against unfamiliar code.
- `git-clone-and-run` — for the SBC path. After `GitDeploy` lands
  the workspace and the secrets file, this script just invokes
  `params.entry` with `params.args` and propagates the exit code
  (0 → pass, non-zero → fail). The caller's repo decides what
  counts as a passing test.

Both built-ins still capture serial, dmesg (when exclusive), camera
frames, and the standard artifact set, so failure diagnosis isn't
worse than for a curated script — just the *assertion* is the
caller's responsibility. The `trusted-firmware` gate exists because
these scripts are the only path through which caller-controlled
binaries / code execute on bench hardware with bench secrets in
scope; the gate is what makes "trusted users testing unknown
firmware in unknown ways" a tractable trust boundary (§15 OQ15).

### 11.1 Wippersnapper firmware variants

Two variants exist, both speaking ProtoMQ; the bench can target
either with the same job-submission shape.

- **Arduino** — `vendor/wippersnapper-arduino` (the `migrate-api-v2`
  branch of the ai-assisted fork; `upstream` remote points at
  `adafruit/Adafruit_Wippersnapper_Arduino` for upstreaming via PR).
  Targets microcontrollers (ESP32-S2/S3, SAMD51, RP2040). Flashed via
  esptool / uf2-msc / picotool. Consumes `secrets.json` (`io_username`,
  `io_key`, `network_type_wifi.{network_ssid,network_password}`, plus
  the bench's `io_url` / `io_port` overrides — those are the
  top-level broker-host fields the firmware actually parses, per
  `vendor/wippersnapper-arduino/src/provisioning/ConfigJson.cpp`,
  defaulting to `io.adafruit.com` : `8883`). The canonical example
  is `vendor/wippersnapper-arduino/examples/secrets-examples/
  secrets-wifi.json`; this repo's `examples/wippersnapper-arduino/
  secrets.example.json` extends it with the `io_url`/`io_port`
  overrides the controller fills in at flash time.

- **Python** — single-board-computer client, **currently
  private/unreleased**. Not vendored. The Python client uses
  **different** broker-override field names from the Arduino firmware
  (not `io_url`/`io_port`); the exact names will be confirmed once
  the repo is reachable. When it opens up, expect the `displays-v2`
  branch with sub-submodules for `protomq` and
  `wippersnapper-protobufs`, and a similar config flow (`secrets.json`
  for the long-running client, `.env` for pytest runs). The bench
  already covers the Pi-class targets via the Pi 5 broker host (§13).
  Wiring info for SBC targets is reachable today from
  `vendor/protomq/scripts/*.json` — some of those demos are
  Pi-targeted even though the rest are Arduino-targeted.

The secrets-substitution flow (caller renders a `secrets.example.json`
with `envsubst`, uploads alongside firmware, controller validates +
flashes + runs) is documented in `examples/README.md` with a
`workflow_dispatch` reference workflow in
`.github/workflows/example-hil-call.yml`.

### 11.2 Two existing test bodies we integrate with

**`vendor/protomq/scripts/*.json`** — JSON, *not* Python. Each file is
a sequence of `steps` keyed off ProtoMQ topics (`checkin.request`,
`display.addedOrReplaced`, …) with `send` payloads and `waitFor`
gates. They are run by the ProtoMQ broker (Pi5 at `192.168.1.210`,
MQTT port `1884`); the device under test connects, the broker replies
according to the script, and the script asserts log/state at each
step. In v1 the controller doesn't re-implement this — a
`protomq.<script-name>` controller-side test just (a) brings the
device up via the device adapter, (b) points it at the ProtoMQ
broker, (c) tails serial + observes the broker's view of the
exchange, (d) calls pass/fail based on whether the script reached its
terminal step. The JSONs feed the topology importer for wiring info
(§5.7), not the runtime.

**`vendor/hil-detection/tests/`** — pytest suites
(`test_circuitpython.py`, `test_micropython.py`,
`test_wippersnapper.py`) with a `conftest.py` that already drives the
bench (SSH to `rpi-displays`, toggle the USB hub, flash via
`pico_hil_flash.sh`, mount CIRCUITPY, etc). **This SSH-from-an-
orchestrator-into-a-HIL-host pattern is exactly the architecture
we're formalising** — the controller is the orchestrator, the HIL
hosts are the targets, and the fixtures are the prototype of what
the device adapters will do over the host transport. Two concrete
clean-ups port hil-detection's conftest onto the new layer:

1. Drop the hardcoded `RPI_PASSWORD` / `sshpass` path entirely;
   switch to the key-based auth the HIL hosts already have set up
   for the `pi` user.
2. Pull `RPI_HOST` (and the equivalent for SBC tests) from the
   controller-injected host context instead of a module constant,
   so the same fixture can run against any host in the fleet.

The controller's `tests/runner.py` shells into pytest with markers
(`pytest -m circuitpython tests/test_circuitpython.py`) on the
target HIL host, captures the report as the job result, and streams
the live log back through the host transport.

See §15 open question 7 for the manifest ownership choice.

## 12. Security posture

**Controller host**

- Service runs as an unprivileged user (`hil`). Holds the
  per-HIL-host SSH private keys in `/etc/hil/keys/` with mode `0400`,
  owned by the `hil` user.
- No `shell=True` anywhere. All subprocess calls (locally and over
  the host transport) use argv lists with explicit binaries.
- Artifact fetches are restricted to an allow-list of hosts
  (`api.github.com`, `objects.githubusercontent.com`, …) and require a
  `sha256` from the caller; mismatch aborts before any artifact is
  pushed to a HIL host.
- Token storage: argon2id hashes only. Plain token shown once.
- OIDC: verify `aud` against a server-configured value (default
  `hil-controller`), reject tokens older than 10 minutes.
- Per-pool rate limits on job submission to prevent a runaway CI
  matrix from monopolising hardware.
- Audit table records every authenticated request (principal, route,
  job id, **target host**, **secrets profile id**, decision) with a
  30-day retention.

**Secret profiles**

- Profile values are referenced from `secrets-profiles.yaml` as
  `${env:NAME}` only; the YAML file itself contains no plaintext
  credentials. Actual values are read from a systemd
  `EnvironmentFile=/etc/hil/secrets.env` (mode `0400`, owner `hil`)
  loaded once at controller start.
- The controller never echoes a profile's secret values back through
  the API or to the dashboard. `/v1/jobs/{id}` shows only the
  profile *id* used. The audit log records the id, not the values.
- Rendered `secrets.json` / `.env` files are written to the HIL
  host's `/tmp/hil/<job-id>/`, mode `0400`, owner `pi`. On job
  terminal the worker `rm -f`s them before releasing the device.
  The event log records that the wipe happened, never the file's
  contents.
- Profiles with `requires_trusted: true` (e.g. `live-io-prod`) are
  only available to principals carrying the `trusted-firmware`
  capability — a deliberate brake on production-account exposure.
- Exclusive-host jobs that capture `host-dmesg.log` and
  `host-usbmon.pcap` get a sanitisation pass on the way back: any
  byte sequence matching a known-secret pattern (a profile value
  loaded for *this* job) is replaced with `***` before the artifact
  is persisted. Imperfect (binary in pcap may evade naive matching),
  but a basic filter against accidental log-disclosure.

**HIL hosts (rpi-displays, rpi-hil00N)**

- `pi` user authenticates with the controller's per-host public key
  only; password auth disabled, the hardcoded bench-default password
  in `vendor/hil-detection/tests/conftest.py` (`RPI_PASSWORD` constant —
  value redacted from this repo's docs)
  is rotated out as part of the cutover (see §15 OQ8).
- known_hosts pinning on the controller side so a man-in-the-middle
  swap can't hijack a HIL session.
- udev rules grant `pi` access to the specific `/dev/serial/by-id/...`,
  USB-IP control node, MCP23017 I²C bus, and the
  solenoid-hub HID device on rpi-displays. No `sudo`.
- Each HIL host runs a long-lived `usbip-autobind` udev rule + a
  short-lived per-job pytest/flasher invocation. There is no
  persistent agent in v1.

## 13. Deployment

Three tiers:

1. **Controller host** — an independent Linux box (Pi, NUC, VM,
   whatever). Requirements: network reachability to every HIL host
   and to `pi5-protomq`, Python 3.11+, disk for SQLite + job
   artifacts, ability to bind an HTTPS port. Does **not** have any
   DUTs attached.
2. **HIL host fleet** — `rpi-displays` for microcontroller DUTs,
   `rpi-hil001` … `rpi-hil007` for SBC DUTs. Each runs a `pi` user
   the controller SSHes into.
3. **ProtoMQ broker** — `pi5-protomq` (`192.168.1.210`, MQTT `1884`,
   web UI `5173`). DUTs talk to it as MQTT clients during
   protomq-flavoured tests. The controller observes the broker but
   does not host it. Not strictly required for non-protomq tests.

**Controller host install** (concretely):

- systemd unit running `uvicorn hil_controller.main:app` bound to
  `127.0.0.1`. Caddy or nginx in front terminates TLS and serves the
  dashboard over the LAN.
- SQLite file in `/var/lib/hil/` with WAL mode. Daily
  `sqlite3 .backup` to a sibling file; logs and artifacts under
  `/var/lib/hil/jobs/<id>/`.
- Hosts, devices, pools, and OIDC policy live in `/etc/hil/` as
  YAML, watched for changes and reloaded without a restart.
- Per-HIL-host SSH private keys in `/etc/hil/keys/`, mode `0400`,
  one file per host (or one shared key for the SBC fleet if that's
  how the bench is set up).

**HIL host prep** (per host, one-off; will be a `deploy/setup-hil-host.sh`
that the controller pushes via SSH):

- Authorise the controller's public key for the `pi` user.
- Install `vendor/usbip-autoattach/server/` (udev rule +
  `usbip-autobind` helper, `usbipd -D` running, `usbip_host` module
  pinned).
- On `rpi-displays`: install `vendor/hil-detection/usb_hub.py` and
  `solenoid_hub_control.py` under `/opt/hil/`, plus the udev rules
  for the MCP23017 and the stable `/dev/serial/by-id/...` symlinks.
- On `rpi-hil00N`: same `pi`-user SSH setup; per-port power-control
  bits land when that hardware does.
- Install Python (system or virtualenv) sufficient for the pytest
  suites in `vendor/hil-detection/tests/` to run.

Single-binary install isn't a goal; the controller is installed via
the Python package + the deploy scripts.

## 14. Observability

- Structured logs (JSON) to stdout, captured by journald.
- `/metrics` (Prometheus) — queue depth, jobs by state, per-device
  utilisation, flash duration histograms, OIDC verification failures.
- Dashboard has a "live tail" panel per job using HTMX SSE on top of
  the same event-log endpoint.

## 15. Design decisions (formerly open questions)

The original sixteen open questions have been resolved by the
stakeholder. Each entry below preserves the original question for
context, captures the stakeholder's verbatim directive, and notes
the implementation strategy. Section number kept stable so existing
cross-references (e.g. "§15 OQ11") still resolve.

1. **Artifact provenance.** Originally: signed attestation vs.
   caller-supplied sha256?
   **Directive:** "We should support the minimum possible
   initially, and include extension points and comments for the
   more effective final solution like attestation, should we wish
   to implement it."
   **Implementation:** caller-supplied sha256 + host allow-list for
   v1 (§12 already wires this). Add a clearly-marked extension
   point in the verification module so swapping in
   `actions/attest-build-provenance` later is a one-class change.

2. **Concurrent jobs on the same device family.** Originally: are
   per-device locks enough, or do we need named shared-resource
   locks (e.g. a camera covering multiple positions)?
   **Directive:** "[A] camera can cover multiple devices, and so
   it would be streaming to a central place, and any test that
   utilized frames of that video would effectively request a
   start and end time stamp and the region of interest to
   capture, which should be stored by the HIL controller, as it
   is aware of and in charge of setting up regions of interest
   per device on the camera."
   **Implementation:** centralised camera stream pipeline owned
   by the controller. Per-device ROI configuration lives in the
   topology manifest (§5.5 aux records grow a `roi` block when
   the aux's `observability` is `camera`). The job event log
   records `(start_ts, end_ts, roi)` against the central stream;
   the `CameraCapture` adapter pulls only that window/region back
   as the per-job artifact. **Replaces** the previous "per-host
   camera `copy_from`" sketch in §10.2.

3. **Result reporting back to GitHub.** Originally: GitHub App
   check-runs vs long-poll-only?
   **Directive:** "I'm not sure yet, but I do want the results to
   be quite exhaustive and include the video links, the serial
   logs, and any other appropriate artifacts, along with the
   test results."
   **Implementation:** v1 keeps long-poll-only result delivery,
   but the response schema (§5.4 job event log + §7 endpoints)
   carries an exhaustive `artifacts[]` array — serial log refs,
   camera window refs, dmesg/usbmon when exclusive, exit codes,
   and anything else the test script chooses to attach.
   Check-run posting remains deferred; do not block on it.

4. **USB-IP topology / force-recover.** Originally: when a Pi
   exports a device, can the API grab it back?
   **Directive:** "But we should always be able to forcefully
   recover, should a test device or bound USB device fail in an
   unresolvable situation."
   **Implementation:** an API-driven force-recover endpoint —
   `POST /v1/devices/{id}/recover` (admin-gated) — that
   (a) cancels any in-flight job holding the device,
   (b) clears the device lock and any held mux/host locks,
   (c) issues a clean detach via `usbip-autoattach` and a
       power-cycle via the solenoid hub,
   (d) re-runs the device health probe.
   Same shape applies at the host level
   (`POST /v1/hosts/{id}/recover`). Audit log captures the
   operator + reason.

5. **Camera artifacts: duty cycle.** Originally: always-on vs
   capture-on-trigger?
   **Directive:** "We probably want to start the camera briefly
   before any test. If we receive a request for another test
   queued up, then we potentially don't stop the camera until
   all the tests are finished running plus a couple of seconds."
   **Implementation:** the camera controller is state-aware.
   Pre-roll for a configurable lead (default ~2s) before the
   first queued job on a covered position; keep the stream alive
   while the queue against that position is non-empty; add a
   trailing buffer (default ~3s) before teardown. The central
   stream from OQ2 makes this cheap — one writer, many
   job-event windows.

6. **Snapper-Python / Snapper-Arduino flashing tooling.**
   Originally: confirm exact toolchain so flasher adapters can
   be specced.
   **Directive:** "That's what the HIL testing repo does in the
   vendor folder. It should include the appropriate flashing
   technology for all of the Arduino, SBCs, for Python, and any
   other test devices."
   **Implementation:** the flasher adapter family in §10.2 wraps
   the scripts already in `vendor/hil-detection` —
   `pico_hil_flash.sh`, `usb_hub.py`, the per-device flashing
   helpers there. Do not reimplement flashing logic locally;
   call into the vendored scripts via the host transport.

7. **Topology source of truth.** Originally: own a primary
   manifest with importers as drift detection, or follow
   upstream files?
   **Directive:** "The system should own a primary manifest with
   the importers and upstream effectively acting as drift
   detection and new changes."
   **Implementation:** `/etc/hil/topology.yaml` is canonical. The
   `protomq_scripts.py` and `hardware_md.py` importers in
   `src/hil_controller/topology/importers/` run as drift
   detectors — they parse the upstream files and **flag**
   divergences in the dashboard / audit log, they do not
   overwrite the manifest. Adding a new board means editing the
   manifest by hand (or accepting a drift-detector suggestion).

8. **Hardcoded password in hil-detection conftest.**
   **Directive:** "We can consider this one acknowledged and
   accounted for."
   **Implementation:** acknowledged. A separate upstream PR
   against `tyeth-ai-assisted/hil-detection` will switch the
   conftest to key-based auth and parameterise the host from
   the controller's transport context. Do not hot-fix inside
   this repo.

9. **Solenoid map channel 3.** Originally: "UNCONFIRMED" channel
   — populate or retire?
   **Directive:** "Assume all solenoid channels are working."
   **Implementation:** topology seed treats channel 3 as
   available. Health probe failures will surface real issues if
   any; no special-case quarantine.

10. **Python Wippersnapper repo access.** Originally: vendor it
    when reachable, with what dual-push pattern?
    **Directive:** "Let's skip over that for now."
    **Implementation:** deferred. No submodule, no template
    changes. Re-open when the repo opens up.

11. **Host transport: SSH vs HTTP agent.** Originally: SSH for
    v1, agent later?
    **Directive:** "I prefer HTTP versus SSH, but we should
    facilitate both."
    **Implementation:** the existing `HostTransport` Protocol in
    `src/hil_controller/hosts/base.py` already abstracts both.
    v1 ships `ssh.py` (already in tree). **Add `agent.py` as the
    preferred transport** — small Python service per HIL host,
    HTTPS API, mTLS or controller-signed token auth, exposing
    `exec` / `stream` / `copy_to` / `copy_from` /
    `healthcheck` one-to-one with the protocol. Per-host config
    picks which transport is used; SSH stays as the fallback
    for hosts where the agent isn't installed yet.
    **Deviation from the originally-shipped M3 SSH-only
    decision; needs a follow-up implementation pass.**

12. **Artifact transfer to HIL hosts.** Originally: controller
    pulls then pushes, vs each host fetches directly?
    **Directive:** "We might need to test both methodologies,
    but we should at least consider the fact the host
    controller is the one in charge collecting firmwares, and
    the others may have restricted internet access."
    **Implementation:** support both in code; **default is
    controller-pulls-then-pushes**. A per-host config flag
    (`fetch_locally: false` by default) lets specific hosts opt
    in to direct fetch when they have outbound. Restricted-
    network hosts work out of the box because the controller is
    their only required egress.

13. **Per-host concurrency caps.**
    **Directive:** (prior contextual rule.) SBC hosts run one
    job or suite at a time; microcontroller host runs many
    bounded only by per-device locks.
    **Implementation:** already in §5.1 `max_concurrent_jobs` +
    §9 per-host semaphore; M3 scheduler plumbed.

14. **SBC test execution shape.**
    **Directive:** "This will follow the git deploy adapter
    approach, handling cloning, rendering secrets, and running
    the tests on the remote host."
    **Implementation:** `GitDeployAdapter` already shipped at
    `src/hil_controller/adapters/git_deploy.py` (M4.5). Fills
    the flasher slot in §6 for `payload.kind == "git-source"`.

15. **Trust model and arbitrary-firmware forensic storage.**
    Originally: `trusted-firmware` capability gates permissive
    scripts; do we additionally snapshot every trusted binary /
    clone for forensic review, and is `exclusive.host`
    required?
    **Directive:** "In an ideal world, yes, for up to thirty
    days, or until capacity reaches seventy-five percent."
    **Implementation:** every permissive-script job (caller has
    `trusted-firmware` capability) snapshots its payload —
    firmware sha256 + binary blob, or the cloned-tree archive —
    to `/var/lib/hil/forensic/<job-id>/`. A retention daemon
    (`src/hil_controller/forensic.py`, to be written) sweeps:
    (a) **30-day cap**: delete entries with `finished_at` older
        than 30 days,
    (b) **75% volume cap**: if `/var/lib/hil` partition exceeds
        75% usage, evict oldest-first until under that mark.
    Whichever triggers first wins. Audit log records every
    eviction. `exclusive.host: true` is **recommended but not
    required** for permissive scripts — the snapshot is the
    durable record; dmesg/usbmon under exclusive is diagnostic
    gravy on top.

16. **Live IO production access from the bench.**
    **Directive:** "That's up to the test caller to decide, but
    I'm assuming they will provide us with appropriate test
    accounts for any system they are contacting. Alternatively,
    if it's a local-only test, then the secrets are immaterial,
    and that's what we'll probably be doing initially."
    **Implementation:** controller does not enforce a
    destructive-action deny-list against the protomq exchange —
    caller's chosen `secrets_profile` (§5.8) supplies the AIO
    account and the caller is responsible for that account
    being appropriate (dedicated test account, rate-limit sane,
    etc). Default posture is local-only: the `bench-protomq`
    profile points at `pi5-protomq:1884` and the bench works
    end-to-end without any live-IO credentials at all.
    `live-io-test` / `live-io-prod` profiles are policy-gated
    (§8) but their contents are caller-supplied.

## 16. Milestones

Status key: **[done]** shipped, **[partial]** partially implemented, **[open]** not started.

- **M0** [done] — pyproject, FastAPI app factory, `/healthz` `/readyz`, SQLite init,
  `hil-controller-ci.yml` CI. Branch: `claude/m0-m1-hil-controller-impl`.

- **M1** [done] — jobs, events SQLite schema; `POST /v1/jobs`, `GET /v1/jobs/{id}`,
  `GET /v1/jobs/{id}/wait` long-poll, `POST /v1/jobs/{id}/cancel`; in-process asyncio
  scheduler + EventBus; fake adapter worker driving the full state machine.
  23 unit tests pass (TDD). HTMX dashboard not yet built.

- **M1.5** [done] — topology seeder (upsert from `topology.yaml` at startup); hosts,
  devices, auxes, connections, audit_log DB tables; `GET /v1/hosts`, `/v1/hosts/{id}`,
  `GET /v1/devices` (filters: host/kind/model/capability/pool), `/v1/devices/{id}`,
  `GET /v1/aux` (filters: kind/capability/pool), `/v1/aux/{id}`,
  `GET /v1/topology` (full graph), `POST /v1/topology/resolve` (dry-run selector,
  409 with structured rejection on no match). `protomq_scripts.py` and
  `hardware_md.py` importers remain [open].

- **M2** [partial] — bearer-token auth: static bootstrap token (`HIL_STATIC_TOKEN`)
  + argon2id DB tokens. `require_auth` now returns a `Principal` dataclass carrying
  `allowed_pools`, `allowed_profiles`, `default_profile`, `capabilities`. Job
  submission gates on pool (403), profile (403), and `trusted-firmware` capability
  for the `raw-firmware-smoke` script. `submitted_by`/`repo` stamped from Principal.
  `audit_log` written on `job.submit`, `job.cancel`, `auth.fail`.
  `mint-token.py` extended with `--allowed-pools` / `--allowed-profiles` /
  `--default-profile` / `--capabilities`. token_id uses `token_hex` (no `_`).
  GitHub OIDC verifier and policy file not yet implemented.

- **M2.5** [partial] — per-job secrets materialisation shipped. `JobRequest.secrets`
  accepts a flat `dict[str, str]`; `GitDeployAdapter` materialises them as env vars
  (`"env"`, default), `secrets.json` (`"json"`), or `.env` (`"dotenv"`) based on
  `params.secrets_format` — formats can be combined with `+`. `JobWorker` redacts
  all values to `"***"` in the DB the moment its `finally` block runs (before the
  terminal-state event), so no plaintext secrets persist at rest.
  Still open: named secret profiles YAML (`bench-protomq` / `live-io-test` /
  `live-io-prod` bundles); `${env:...}` server-side resolver; nested secrets.json
  values (WiFi credentials etc.).

- **M3** [done] — `SSHTransport` (`asyncssh`, key auth, per-call connections).
  `RealHostRegistry` loads `topology.yaml` and returns SSH-backed adapters.
  Known-hosts pinning and connection pooling deferred (open question 11).

- **M3.5** [open] — first real MCU adapter chain against `rpi-displays`.
  Three concrete adapters, plus the operator-facing leases/serial surfaces
  needed to *use* them without SSH. Exercises `exclusive_device` /
  `exclusive_hub` end-to-end on production hardware.

  **Scope:**

  1. **`SerialCaptureAdapter`** (`adapters/serial_capture.py`).
     - Opens a streaming channel against the host's
       `/dev/serial/by-id/usb-Adafruit_<board>_<iSerial>-if00` (stable
       across re-enumeration — never `ttyACM*` numbering). On hosts using
       usbip, the channel opens on the *attached* side (controller),
       resolved at start time from `UsbipBridge.list_serial_ports`.
     - Two phases: **boot capture** (from `acquire()` until first prompt
       / quiet window, default 5 s) and **run capture** (concurrent with
       the run phase, terminated by `release()`). Both line-buffered,
       tee'd to (a) `JobEvent` `log` events streamed over long-poll and
       (b) an on-disk artifact `serial-<phase>.log` in the job's artifact
       dir.
     - Driven by `socat OPEN:/dev/serial/by-id/<...>,b115200,raw -`
       inside the transport (SSH or Local). Baud + parity come from the
       device manifest (`device.serial.{baud, bits, parity, stopbits}`,
       default 115200/8N1).
     - Mid-test API: the test script can call
       `adapter.serial.read_until(token, timeout=...)` against a buffered
       reader without losing concurrently-streamed bytes.
     - Pure parser surface (`parse_boot_marker`, `split_lines`)
       unit-testable without hardware; integration test on `rpi-displays`
       gated by `HIL_LIVE_BENCH=1`.

  2. **`Flasher` chain** (`adapters/flashers/`).

     Not a single esptool adapter — a Protocol with a CLI-tool-wrapper
     base class, so every flasher family that
     `vendor/hil-detection/scripts/pico_hil_flash.sh` currently dispatches
     to (and the families that script explicitly flags as
     future-target — `esptool.py` for ESP, `bossac` for SAMD51/SAMD21,
     `picotool` for RP2040/RP2350) maps cleanly onto a subclass.

     ```python
     # adapters/flashers/base.py

     class FlasherProtocol(Protocol):
         name: str                              # "esptool" / "picotool" / "uf2_msc" / ...

         async def probe(self) -> ChipInfo: ...           # chip family, mac, flash size
         async def erase(self) -> None: ...
         async def flash(self, artifact: Artifact) -> FlashResult: ...
         async def reset(self, *, into: Literal["bootloader", "application"]) -> None: ...

     class CliFlasher(FlasherProtocol):
         """Base for flashers that drive an external CLI tool.

         Provides:
           - `_run(argv, *, env=None, timeout=None) -> ExecResult` via the
             owning HostTransport.
           - `_locate(tool_name) -> PurePosixPath` with PATH probe + cache.
           - structured-error wrapping (`FlasherToolMissing`,
             `FlasherToolFailed`, with stdout/stderr captured).
           - serial-port handoff: the CLI tool gets exclusive access to the
             port for the duration of the call; SerialCaptureAdapter is
             paused via a shared `port_lock` and resumed on `finally`.

         Subclasses override `name`, the argv builders for each verb, and
         the per-verb output parsers.
         """
     ```

     **Concrete subclasses:**
     - **`EsptoolFlasher(CliFlasher)`** — **M3.5 ships this one.**
       Wraps `esptool.py` (chip-id / read-mac / flash-id / erase-flash /
       write-flash, multi-segment bin/elf input). Used by the existing
       `ArduinoWsExecAdapter` flash phase for the raw-`.bin`
       `raw-firmware-smoke` case (PlatformIO upload still goes through
       `pio run --target upload`), and optionally by
       `UsbFingerprintAdapter` for a `chip-id` confirmation step gated
       by `usb_fingerprint.confirm_with_esptool=true`.
     - **`TinyUf2Fetcher`** (`adapters/tinyuf2_fetcher.py`) +
       **`tinyuf2-install` job kind** — also M3.5. The fetcher
       resolves `board_name` → asset URL via the
       `adafruit/tinyuf2` GitHub Releases API, falling back to a
       chip-family asset (e.g. `tinyuf2-feather_esp32s3_reverse_tft-…`
       → `tinyuf2-esp32s3-…` if the board-specific zip is missing for
       that release). Downloads the asset zip, extracts `combined.bin`
       plus the metadata, and hands the path to `EsptoolFlasher.erase()`
       + `EsptoolFlasher.flash(artifact)` (offset `0x0` for ESP32 /
       ESP32-S2, `0x0` for S3 — esptool's combined.bin always lives at
       the start). Pinned release tag is optional (default = latest
       stable); the resolved tag + asset digest are written into the
       job event log for reproducibility.
     - **`PicotoolFlasher(CliFlasher)`** — M4. Wraps `picotool` and
       owns the three-stage BOOTSEL entry strategy already encoded in
       `vendor/hil-detection/scripts/pico_hil_flash.sh` (force-reboot →
       1200-baud sentinel → `machine.bootloader()` REPL fallback).
     - **`BossacFlasher(CliFlasher)`** — M4. Wraps `bossac` for
       SAMD51/SAMD21 (typically paired with `SolenoidHubAdapter
       .samd51_uf2` for bootloader entry).
     - **`Uf2MscFlasher(FlasherProtocol)`** — M4. *Not* a CliFlasher —
       mounts the BOOTSEL MSC drive, copies `.uf2`, `sync`, unmounts.
       Backed by a transport `exec()` call rather than a CLI tool.
       Used by SAMD51 CircuitPython payloads and by RP2040/RP2350
       CircuitPython payloads. Also the natural fit for STM32/nRF
       CircuitPython payloads when those land (most CircuitPython
       boards expose the BOOTSEL/DFU-disabled flow as MSC).
     - **`DfuUtilFlasher(CliFlasher)`** — planned. Wraps `dfu-util` for
       STM32 / nRF52 CircuitPython boards (and any board with a DFU
       bootloader rather than an MSC bootloader). Not on the M4
       critical path because no current DUT requires it, but the
       Protocol + base class shipping in M3.5 means it's a single-file
       add when the first STM32/nRF target lands. This platform is a
       general HIL controller, not WipperSnapper-specific — the
       CircuitPython target matrix is broader than the WS-target
       matrix below.
     - **`NoOpFlasher(FlasherProtocol)`** — pre-provisioned devices;
       all verbs return success without touching hardware. Useful for
       SBCs and for jobs that only run the test phase.
     - **`PioUploadFlasher(CliFlasher)`** — wraps `pio run --target
       upload`; lets the existing `ArduinoWsExecAdapter` route through
       the same Flasher Protocol as the raw-bin path, so the worker has
       a uniform `flasher.flash(artifact)` call site regardless of
       payload kind.

     **Host placement (any subclass):** runs wherever the live serial
     port lives — rpi-displays for ship-artifacts mode, the controller
     after a `UsbipBridge.attached()` context manager has entered for
     usbip mode. The flasher is constructed with `(transport,
     serial_port)`, not with a device record, so the same instance type
     serves both.

     **Selection:** device YAML carries `flasher: <name>` (one of the
     `name` values above). `RealHostRegistry` resolves the name to a
     subclass at adapter construction time. Multi-stage devices (e.g. an
     RP2040 that needs `picotool` to *flash* but `uf2_msc` for a
     CircuitPython payload) get `flasher_by_artifact_kind:` instead — a
     map from artifact MIME / extension to flasher name. M3.5 ships only
     the flat-string form; the map form lands with M4 when there's more
     than one concrete to switch between.

     **Bootloader-entry note:** entering bootloader is *not* a Flasher
     responsibility. It's owned by `SerialCaptureAdapter` (1200-baud
     sentinel), `SolenoidHubAdapter` (`samd51_uf2` / hard power-cycle),
     or the Flasher's own CLI invocation when the tool handles it
     natively (esptool's GPIO sequence over USB, picotool's
     `reboot -u -f`). The state machine calls `flasher.reset(into=
     "bootloader")` and each subclass decides whether to delegate
     (return immediately and let an upstream adapter handle it) or
     execute (run the tool's reset path).

  3. **`SolenoidHubAdapter`** (`adapters/solenoid_hub.py`).
     - Thin async facade over `vendor/hil-detection/usb_hub.py`'s
       `SolenoidHubController` (synchronous, MCP23017 over I²C at
       `0x20`). Runs on the host that physically owns the MCP23017
       (rpi-displays today); the controller drives it via a transport
       `exec()` call against a small `solenoid_hub_control.py` helper
       in `/opt/hil/` (see §13 deployment).
     - Public surface mirrors the existing protocol expected by
       `UsbFingerprintAdapter` (see `docs/AGENT_HANDOFF.md` USB-identity
       wiring): `async def all_off()`, `async def port_on(channel)`,
       `async def port_off(channel, *, hold_s=0.5)`. Plus the timing
       profile selector: `async def samd51_uf2(channel)` performs the
       short/long pulse sequence that drops a SAMD51 into UF2
       bootloader. Profile chosen by `device.solenoid.profile` (default
       `"standard"`).
     - **Wires the existing M6 provider slot** in `main.py`:
       `app.state.usb_fingerprint_provider` becomes non-no-op, so the
       `/v1/devices/{id}/learn-usb` endpoint and its UI button finally
       depower a real port and capture real VID/PIDs.
     - All eight channels are operational per OQ9 stakeholder directive;
       the adapter does *not* gate writes on a per-channel "known good"
       flag.

  **Composition / phase-machine coupling:**

  - `acquire()` runs in **`preparing`**: optional usbip attach (via
    `UsbipBridge`, M6 already), then `SolenoidHubAdapter.port_on()` if
    the device is solenoid-powered, then `SerialCaptureAdapter` opens
    the boot channel. Lease is `exclusive_device` for normal jobs;
    learn flows escalate to `exclusive_hub` (M6 already enforces the
    conflict matrix).
  - `flash()` runs in **`flashing`**: the device's resolved Flasher
    subclass is invoked through `flasher.flash(artifact)`. M3.5 ships
    `EsptoolFlasher` + `PioUploadFlasher` + `NoOpFlasher`; M4 adds
    `PicotoolFlasher` / `Uf2MscFlasher` / `BossacFlasher` / optional
    `DfuUtilFlasher`. The serial port is *released* by SerialCapture
    for the duration (the CLI tool needs exclusive use; `CliFlasher`
    takes the shared `port_lock`) and *reopened* after flash completes,
    so a flash-then-watch-boot job works without an explicit reset.
  - `reset()` mid-job uses `SolenoidHubAdapter.port_off(hold_s=0.2);
    port_on()` (or `samd51_uf2` for the UF2 path); SerialCapture's
    buffered reader is *not* drained — a test script can assert on
    the post-reset banner without race.
  - `release()` reverses in `finally`: detach usbip (M6), serial
    channels closed, solenoid left in whatever state the manifest
    declares as the resting state (`device.solenoid.resting`, default
    `on`).

  **Operator surface (the "you can't have serial management without
  leases" piece):**

  - **`/ui/leases`** — new HTMX page listing all active
    `device_leases` (kind, device, host, holder job id with link,
    acquired_at, expires_at). Force-release button (admin-gated,
    POST `DELETE /v1/leases/{id}`) for stuck states. Auto-refresh
    every 3 s via HTMX. Surfaces the `/v1/leases` endpoints that have
    existed since M6 but were never given a UI.
  - **Inline lease badge** on `/ui/devices` and `/ui/devices/{id}`:
    "locked by job #N" with link, or "free". Sourced from the same
    leases query; no new endpoint.
  - **Per-host exportable busids panel** on `/ui/hosts/{id}` — runs
    `sudo -n /usr/sbin/usbip list -l` over the host transport, caches
    for 5 s, renders busid → VID/PID → matched-device-name. Operator
    can see "rpi-displays exports 8 busids, 1-1.1.1.4 maps to
    `mcu-feather-esp32s3-revtft`" without SSH. New endpoint:
    `GET /v1/hosts/{id}/usbip/exportable`. Read-only — no bind/unbind
    buttons in M3.5 (those go in M4 alongside the manual usbip
    troubleshooting flow).
    **Current state (2026-06-08):** this endpoint is implemented, but it
    is intentionally discovery-only. `POST /v1/leases` is the ownership
    primitive for "who may touch this DUT or hub right now"; it does not
    itself bind, attach, detach, or otherwise export USB over the
    network. Public mutation routes for USB-IP remain M4 work.
  - **Live serial tail** on `/ui/jobs/{id}` — extend the existing job
    detail page with a serial-log pane that subscribes to the
    long-poll event stream and renders `log` events whose source is
    `serial_capture`. No new transport; just a template change.
  - **Manual reset button** on `/ui/devices/{id}` — POSTs to a new
    `POST /v1/devices/{id}/reset` (admin-gated, requires no active
    lease *or* the operator's own lease). Calls
    `SolenoidHubAdapter.port_off; port_on` with the device's profile.
    Useful for un-sticking a DUT outside a job.

  **WipperSnapper target matrix (all first-class):** WipperSnapper
  supports four MCU families today, all of which the controller must
  drive. The controller is **not** WipperSnapper-specific — it's a
  general HIL platform — so the broader CircuitPython matrix (STM32 /
  nRF / etc) is also planned, just not on the M4 critical path.

  | Family            | Bootloader entry           | Flash tool       | Milestone | Notes |
  |-------------------|----------------------------|------------------|-----------|-------|
  | ESP32 (S2/S3/...) | esptool GPIO sequence      | `EsptoolFlasher` | M3.5      | First concrete; revtft Feather already usbip-bound. |
  | RP2040            | 1200-baud / picotool -u -f | `PicotoolFlasher` / `Uf2MscFlasher` | M4 | Same CLI / same BOOTSEL chain as RP2350. |
  | RP2350            | 1200-baud / picotool -u -f | `PicotoolFlasher` / `Uf2MscFlasher` | M4 | RP2354+PSRAM variants share this row. |
  | SAMD51            | double-tap (samd51_uf2)    | `BossacFlasher` / `Uf2MscFlasher`   | M4 | Solenoid-driven double-tap for bootloader entry. |
  | STM32 (future CP) | `dfu-util` detach          | `DfuUtilFlasher`              | post-M4   | Planned, not critical path; lands when first STM32 CP DUT arrives. |
  | nRF52 (future CP) | `dfu-util` detach / SWD    | `DfuUtilFlasher` / `Uf2MscFlasher` | post-M4   | Same — opens up Adafruit Feather nRF52 family. |

  **Non-goals for M3.5** (held for M4):
    - `PicotoolFlasher` / `Uf2MscFlasher` / `BossacFlasher` concretes —
      protocol + base class ship in M3.5, the per-family CliFlasher
      subclasses arrive in M4 (see the target matrix above).
    - `flasher_by_artifact_kind:` map form on the device YAML — flat
      `flasher: <name>` only in M3.5.
    - Manual usbip bind/attach/detach buttons — M4.
    - Hardcoded-password (`vendor/hil-detection/tests/conftest.py`
      `RPI_PASSWORD`) cleanup (OQ8) — M4.
    - Per-host SBC power control (rpi-hil00N solenoid mux) — later.

  **Test plan:**
    - Parser-level units: `parse_boot_marker`, `split_lines`,
      `EsptoolFlasher.parse_chip_id`, `EsptoolFlasher.parse_mac`,
      `SolenoidHubAdapter._encode_mask`, `CliFlasher._wrap_exec_error`.
    - Protocol-conformance unit: a `FlasherProtocol` contract test that
      every concrete subclass (`EsptoolFlasher`, `PioUploadFlasher`,
      `NoOpFlasher` in M3.5) is run against — guarantees the four-verb
      surface stays uniform as M4 adds more.
    - Adapter-level units against fake transports (extend
      `tests/transport/fake.py`).
    - End-to-end smoke against `rpi-displays` gated by
      `HIL_LIVE_BENCH=1` (CI skips, operator runs locally): boot
      capture on `1-1.1.1.4` survives a usbip attach + detach;
      `EsptoolFlasher.probe()` post-attach returns the expected
      ESP32-S3 chip family + a non-zero MAC; `SolenoidHubAdapter
      .port_off; port_on` flips the channel and the serial port
      re-enumerates.
    - UI tests via `httpx.AsyncClient` for the three new templates and
      the two new endpoints.

- **M4** [open] — close the WipperSnapper target matrix: per-family
  Flasher concretes for RP2040 / RP2350 / SAMD51, manual usbip
  management surface, solenoid-hub reset orchestration on top of the
  M3.5 `SolenoidHubAdapter`, and OQ8 hardcoded-password cleanup.

  **Scope:**

  1. **`PicotoolFlasher(CliFlasher)`** (`adapters/flashers/picotool.py`).
     - Wraps the `picotool` CLI for both RP2040 (`2e8a:0003` BOOTSEL)
       and RP2350 (`2e8a:000f` BOOTSEL). Same binary, same argv shape,
       different default flash sizes (2 MB / 4 MB) parsed from
       `picotool info -a "type:"`.
     - **Owns the three-stage BOOTSEL entry strategy** already
       battle-tested in
       `vendor/hil-detection/scripts/pico_hil_flash.sh:259-320`:
       (1) `picotool reboot -u -f` (force-reset any RP app, including
       MicroPython), (2) `stty -F <port> 1200 cs8 -cstopb -parenb`
       (CDC 1200-baud sentinel for CircuitPython / WipperSnapper /
       C SDK), (3) `python -c 'import machine; machine.bootloader()'`
       sent over the REPL (MicroPython fallback — its USB stack ignores
       `SET_LINE_CODING`). Driven from `flasher.reset(into=
       "bootloader")`; each strategy waits for sysfs re-enumeration
       via `wait_for_reenumeration` (port path stays stable, VID/PID
       changes).
     - `flash()` calls `picotool load --force` then
       `picotool reboot -a`; the re-enumeration helper waits for the
       application VID/PID before returning so SerialCapture can
       reopen the port without a race.
     - `erase()` queries flash size via `picotool info -a` (parser
       lives in pure-function module — `parse_flash_size`,
       `parse_chip_type`), defaults RP2040→2 MB / RP2350-family→4 MB,
       then `picotool erase -r <base> <end> --force`.
     - `probe()` returns `ChipInfo(family="RP2040"|"RP2350"|...,
       flash_bytes=..., unique_id=...)` parsed from `picotool info -a`.

  2. **`Uf2MscFlasher(FlasherProtocol)`** (`adapters/flashers/uf2_msc.py`).
     - Mounts the BOOTSEL mass-storage drive, copies the `.uf2`,
       calls `sync`, unmounts. All five steps over the host transport.
     - Bootloader entry is *not* this adapter's job — the device must
       already be in BOOTSEL by the time `flash()` is called. Pair
       with `PicotoolFlasher.reset(into="bootloader")` for
       RP2040/RP2350 (which leaves the chip in BOOTSEL after the
       reset) or with `SolenoidHubAdapter.samd51_uf2(channel)` for
       SAMD51.
     - Locates the MSC mount point by lsblk label match
       (`RPI-RP2`, `RP2350`, `CIRCUITPY`, configurable per device via
       `device.uf2_msc.label`). If unmounted, `mount /dev/sdX1
       /mnt/<label>` — udev usually auto-mounts, so we try the udisks
       path first and only fall back to manual mount on failure.
     - `erase()` not supported (no equivalent for MSC); returns
       `FlasherUnsupported`. `probe()` returns the label + filesystem
       size as best-effort `ChipInfo` (no chip family — the MSC
       protocol doesn't reveal it; comes from the device YAML).

  3. **`BossacFlasher(CliFlasher)`** (`adapters/flashers/bossac.py`).
     - Wraps `bossac` for SAMD51 (and SAMD21 — same CLI). Verbs:
       `--erase`, `--write`, `--verify`, `--reset`.
     - Bootloader entry: paired with
       `SolenoidHubAdapter.samd51_uf2(channel)` which performs the
       short/long pulse sequence the chip's ROM treats as a double-tap
       reset. The Flasher's own `reset(into="bootloader")` delegates
       to the solenoid adapter when one is provided; otherwise raises
       `FlasherNeedsExternalReset` so the operator UI surfaces a
       clear "manual reset required" state.
     - `probe()` returns `ChipInfo(family="SAMD51"|"SAMD21", ...)`
       parsed from `bossac --info` output.

  4. **Manual usbip management surface** (`/ui/hosts/{id}` + new
     endpoints, building on the M3.5 read-only exportable-busids
     panel):
     - **Status (2026-06-08):** planned, not implemented. Current code
       exposes `GET /v1/hosts/{id}/usbip/exportable` plus
       controller-managed/internal use of `UsbipBridge`; there is no
       public bind/unbind/attach/detach route yet.
     - **Bind/unbind buttons** on the exportable-busids panel:
       `POST /v1/hosts/{id}/usbip/bind {busid}` /
       `POST /v1/hosts/{id}/usbip/unbind {busid}`. Admin-gated; respect
       active `device_leases` (refuse to unbind a busid that maps to a
       leased device, with a force-override checkbox for stuck states).
     - **Attach/detach panel** on the controller's host page:
       `GET /v1/hosts/controller/usbip/attached` parses `usbip port`,
       `POST /v1/hosts/controller/usbip/attach {remote_host, busid}`,
       `POST /v1/hosts/controller/usbip/detach {port}`. Lets an
       operator stand up a usbip-attached DUT without running a job.
       If we later need an unmanaged or third-party consumer (not the
       controller host itself), this likely wants to grow into a
       first-class export-session resource rather than reusing
       `/v1/leases` alone: the lease records ownership, while the export
       session would record transport state (`server_addr`, `busid`,
       consuming host/app, attached port, expiry / revocation).
     - **vhci status badge** on `/ui/hosts/controller`: shows whether
       `vhci_hcd` is loaded, how many ports are populated, last error
       from the kernel ring buffer (parsed from `dmesg -t -k | grep
       vhci_hcd`, cached 10 s).
     - **Re-enum smoke button** on `/ui/devices/{id}`: runs the
       handoff doc's "Model A re-enumeration risk" validation
       sequence end-to-end — bind on hub host, attach on controller,
       `probe()` twice to exercise two re-enum cycles, detach. Gated
       by an `exclusive_hub` lease the button acquires for the
       operator's session. Captured stdout/stderr surface as a
       toast + persistent artifact under the host record.

  5. **Solenoid-hub reset orchestration** (`adapters/reset_strategy.py`
     + extensions to `RealHostRegistry`):
     - First-class `ResetStrategy` registry that composes from the
       M3.5 building blocks: `SerialOneShot1200` (CDC sentinel),
       `Picotool ForceReset` (calls `picotool reboot -u -f`),
       `SolenoidPower Cycle` (port_off + port_on),
       `SolenoidDoubleTap` (samd51_uf2), `EsptoolBootloaderGpio` (no-
       op — esptool handles it inline), `MachineBootloaderRepl`
       (MicroPython fallback).
     - Selection from `device.reset.strategy` (single name) or
       `device.reset.chain` (ordered list — try each, succeed on first
       to produce the target VID/PID). Default chains baked in per
       chip family so most devices need no explicit config.
     - Recovery path: `POST /v1/devices/{id}/recover` (OQ4) runs the
       full chain (`exclusive_hub` lease → solenoid all-off → solenoid
       on target → reset chain → re-probe → release). UI button on
       `/ui/devices/{id}`.

  6. **OQ8 — hardcoded-password cleanup**:
     - `vendor/hil-detection/tests/conftest.py:RPI_PASSWORD` (value
       deliberately not duplicated here; see the submodule file) is
       replaced with a lookup against the new `HIL_BENCH_TOKEN` env
       var. The hil-
       detection PR ships separately (this repo can't hot-fix a
       submodule), but the controller side gains a one-shot
       provisioner: `scripts/setup-hil-detection-secret.sh` writes
       `/etc/hil-detection/credentials.env` on each bench host with
       a freshly-minted argon2id-hashed token, sourced by
       conftest.py. The plaintext value never lands in the repo or in
       this controller's DB.
     - Audit pass over the controller repo for any other hardcoded
       secrets (initial sweep already done — `mint-token.py` +
       `controller.env` are the only secret carriers, both properly
       env-driven).

  **Composition with M3.5:**
    - M3.5 ships the FlasherProtocol contract; M4's three new
      concretes are drop-ins. No worker changes needed beyond
      registering the new names in `RealHostRegistry._FLASHERS`.
    - The reset-strategy registry is new in M4 — M3.5's
      `EsptoolFlasher.reset()` becomes a thin wrapper that picks the
      `EsptoolBootloaderGpio` strategy from the registry instead of
      hardcoding it. (No external behavior change; refactor only.)

  **Non-goals for M4** (held for later, not removed from the roadmap):
    - `DfuUtilFlasher` (STM32 / nRF52 CircuitPython) — planned post-M4
      per the target matrix above. The M3.5 FlasherProtocol +
      CliFlasher base means dropping in a new family is a single-file
      add; we just don't want to write it speculatively without a real
      DUT to validate against.
    - SBC-side per-port power control (`rpi-hil00N` mux).
    - Per-job artifact retention / forensic snapshots (OQ15) — that's
      its own milestone.

  **Test plan:**
    - Parser units for `parse_flash_size`, `parse_chip_type`,
      `parse_bossac_info`, `parse_usbip_port_table`,
      `parse_vhci_status`.
    - Per-flasher Protocol-conformance test (same one used in M3.5,
      extended with the new concretes).
    - Reset-strategy chain unit test (fake serial + fake solenoid).
    - `HIL_LIVE_BENCH=1` smoke on rpi-displays: an RP2040 DUT, a
      SAMD51 DUT, and an RP2350 DUT each round-trip
      probe → erase → flash → reset → probe-confirms-app-mode without
      operator action.
    - UI tests for bind/unbind/attach/detach endpoints + the recover
      flow.


- **M4.5** [done] — `GitDeployAdapter` (clone → setup → run → cleanup over SSH or
  local subprocess). `RealHostRegistry.get_adapter` wires it to SBC jobs via
  `git-clone-and-run`. `deploy/topology.example.yaml` seeds the first SBC host
  config. `LocalTransport` (`hosts/local.py`, asyncio subprocess) added alongside
  `SSHTransport`; topology hosts with `kind: local` use it for localhost SBC jobs
  without SSH. `source.pat` field injects a GitHub PAT into the HTTPS clone URL.
  `GitDeployAdapter` stores captured stdout/stderr in `_run_*` / `_deploy_*`
  attributes; `JobWorker` emits them as `log` kind events on the long-poll stream
  after each phase. `Scheduler` now wired to `RealHostRegistry` in `main.py`
  (previously always fell back to `_FakeAdapter` regardless of topology).
  `examples/wippersnapper-python/job.json` + `scripts/submit-wipper-test.sh`
  provide a ready-to-use non-hardware test submission. 65 tests.

- **M5** [partial] — `ProtoMQObserver` (`adapters/protomq_observer.py`): activates
  a named script on the ProtoMQ broker via HTTP API
  (`POST /api/scripts/{name}/activate`), subscribes to MQTT `#` wildcard and
  forwards all messages as `log` events during the run phase (protobuf topics shown
  as `<protobuf Nb>`, others decoded UTF-8), emits completed steps at teardown.
  Runs as a concurrent `asyncio.Task` alongside the test; cancelled cleanly on
  finish/error/timeout. `aiomqtt>=2.0` added. Configured via
  `params.protomq.{broker_host, mqtt_port, api_port, script}`. 87 tests.
  Still open: camera capture; artifact storage; Prometheus `/metrics`;
  `raw-firmware-smoke` built-in; `live-io-test`/`live-io-prod` profiles;
  Python Wippersnapper submodule (OQ10); protobuf decoding for MQTT messages;
  `GET /v1/jobs/{id}/logs` non-blocking endpoint.

- **M6 — USB identity** [done] — multi-VID/PID per device + hub-port identity
  + exclusivity leases + active/passive USB-ID learn.
  - `device_usb_ids` table (surrogate PK + `UNIQUE (device_id, vid, pid,
    COALESCE(iserial, ''))` expression index — NULL iserials dedup
    correctly). Roles: `runtime | bootloader | dfu | msc | cdc | unknown`.
  - New `devices` columns: `hub_host_id` (defaults to `host_id`),
    `hub_port_path` (sysfs bus-id, the *real* identity), `solenoid_channel`,
    `usb_serial`.
  - Topology YAML `usb_ids:` list (back-compat: legacy `usb:` block still
    seeds one row). Migration backfills any pre-existing `usb_json`.
  - REST: `GET/POST/DELETE /v1/devices/{id}/usb-ids`,
    `POST /v1/devices/lookup-by-usb`, `POST /v1/devices/{id}/learn-usb`.
  - HTMX devices form: list editor, add/delete row, Learn-USB button
    (with optional reset cycle for bootloader+runtime split).
  - `device_leases` table + `queue/leases.py` primitive (atomic acquire
    inside `BEGIN IMMEDIATE`; kinds `exclusive_device` / `exclusive_hub`
    with the full conflict matrix). `GET/POST/DELETE /v1/leases`.
    Startup sweep releases orphan leases of crashed jobs. Scheduler
    acquires `exclusive_device` per job; releases in finally.
    These rows intentionally represent ownership only. They do not imply
    an active USB-IP bind/attach session, and are designed to compose
    with later public USB-IP mutation/export APIs rather than replace
    them.
  - `adapters/usb_scan.py`: `parse_usbip_list`, `learn_once`,
    `passive_learn_loop`. Scheduler runs the loop as a background task
    during the lease so any unseen VID/PID enumerating on the device's
    `hub_port_path` is auto-recorded with `source='passive'`.
  - `adapters/usb_fingerprint.py`: `UsbFingerprintAdapter.learn` —
    acquires `exclusive_hub`, depowers via solenoid, repowers the target
    port, captures VID/PIDs; optional reset cycle splits role into
    bootloader vs runtime.
  - 56 new tests (PR1–PR5); 275 total pass.

Past M6 we revisit dynamic hardware switching, GitHub check-run posting,
and the SSH → agent transport upgrade (open question 11) based on what
we've learned.

## 17. Planned features (deferred)

### 17.1 Multi-stage job: clone → discover → run

Rather than submitting a single monolithic job, a "staged job" flow would:

1. **Stage 1 — Clone**: clone the repo to `jobs_dir/{ts}_{job_id}/source/`, run
   `pytest --collect-only -q`, and emit the discovered test list as a `metadata`
   event on the job.
2. **Stage 2 — Configure**: the web UI presents the collected test list so the
   user can select a subset, confirm env vars, and trigger the run.  The staged
   job persists between stages (new state `awaiting-config`).
3. **Stage 3 — Run**: execute the chosen tests, streaming logs as normal.  All
   three stages share the same `job_id`; their outputs are stored under
   `jobs_dir/{ts}_{job_id}/` with timestamp-prefixed sub-directories.

**Re-triggering**: a completed staged job can be re-run from any stage — the
clone is reused if the ref matches, or re-cloned otherwise.

**Implementation notes**:
- Requires new job states: `awaiting-config` (between stages 1 and 2).
- The scheduler must handle `awaiting-config` as a pause point (not a terminal
  state); a `POST /v1/jobs/{id}/configure` endpoint resumes it.
- `jobs_dir` (env `HIL_JOBS_DIR`) controls where clones land locally (for
  `LocalTransport`); SSH-based jobs still clone to the remote host's `/tmp/hil/`.
- The web UI at `GET /ui/jobs/{id}/configure` shows collected tests and lets the
  user confirm before resuming.

### 17.2 Token management web UI

See §8.1 for the full design note.  Implementation requires `/v1/tokens` CRUD API
guarded by `manage-tokens` capability, and a `/ui/tokens` admin page.
