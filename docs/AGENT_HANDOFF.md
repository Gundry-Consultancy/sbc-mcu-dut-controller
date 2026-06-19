# Agent handoff

Working notes for the next Claude Code agent picking up this project.
Read this **before** asking the user "what is this project?" — most of
the orientation is already in `docs/ARCHITECTURE.md` and the rest is
here.

**Session start:** read `.claude/memory/MEMORY.md` and the files it
indexes for current project state, conventions, and user preferences.
Memories live in the repo (`.claude/memory/`), not in `~/.claude/`.

## What this is

A controller that brokers GitHub-CI hardware-in-the-loop test requests
out to a fleet of Raspberry Pi HIL hosts. CI calls a long-poll HTTP
API; the controller routes work to whichever Pi owns the target
device, runs the test there over SSH, returns pass/fail + artifacts.

## Current state checklist

Done:

- [x] `docs/ARCHITECTURE.md` — full design v0.1+, milestones updated
      with [done]/[partial]/[open] status.
- [x] Four submodules under `vendor/` (see `vendor/README.md`).
- [x] `scripts/setup-submodules.sh` — idempotent post-clone setup.
- [x] `examples/` — caller-side templates. `hil-call.sh` is the
      reference long-poll script.
- [x] `.github/workflows/example-hil-call.yml` — `workflow_dispatch`
      demo covering both auth paths.
- [x] `.github/workflows/ws-python-ci.yml` — reusable workflow for
      Wippersnapper-Python (SBC, `wippersnapper-python` pool,
      `git-clone-and-run`, default `-m eink_large`).
- [x] `examples/ws-python-caller.yml` — caller template.
- [x] **M0** — `pyproject.toml`, FastAPI app, `/healthz`, `/readyz`,
      `hil-controller-ci.yml` CI (lint + pytest).
- [x] **M1** — `POST /v1/jobs`, `GET /v1/jobs/{id}`, long-poll
      `GET /v1/jobs/{id}/wait`, cancel; SQLite jobs + events schema;
      asyncio scheduler + EventBus; fake adapter worker. 23 tests.
- [x] **M1.5** — hosts/devices/auxes/connections/audit_log DB tables;
      topology seeder from `topology.yaml`; `GET /v1/hosts`, `/v1/devices`,
      `/v1/aux`, `/v1/topology`, `POST /v1/topology/resolve`.
- [x] **M2 partial** — `Principal` dataclass; `require_auth` returns
      `Principal`; pool/profile/capabilities gating on job submit (403);
      audit log on submit/cancel/auth-fail; `mint-token.py` extended.
      OIDC and policy file not yet implemented.
- [x] **M3** — `SSHTransport` (asyncssh, key auth).
      `RealHostRegistry` loads `topology.yaml` and returns SSH adapters.
- [x] **M4.5** — `GitDeployAdapter` (clone → setup → run → cleanup).
      Wired to SBC jobs via `git-clone-and-run` script name.
- [x] `deploy/topology.example.yaml`, systemd unit,
      `deploy/controller.env.example`.
- [x] `LocalTransport` (`hosts/local.py`) — asyncio subprocess transport
      for localhost SBC jobs; topology `kind: local` routes to it.
      `source.pat` injects a GH PAT into the HTTPS clone URL.
- [x] Log streaming — `GitDeployAdapter` stores `_run_stdout/_run_stderr/
      _deploy_stdout/_deploy_stderr`; `JobWorker` emits them as `log` kind
      events on the long-poll stream after deploy and run phases.
      `Scheduler` now wired to `RealHostRegistry` in `main.py` (was always
      `_FakeAdapter` before regardless of topology).
- [x] **M5 partial** — `ProtoMQObserver` (`adapters/protomq_observer.py`):
      HTTP script activation, MQTT `#` wildcard subscription, log event
      forwarding, completed-steps summary on teardown. `aiomqtt>=2.0`.
      Configured via `params.protomq.{broker_host,mqtt_port,api_port,script}`.
- [x] `examples/wippersnapper-python/job.json` — ready-to-use job body
      with `params.protomq` block.
- [x] `scripts/submit-wipper-test.sh` — GH PAT + ref substitution via jq,
      calls `hil-call.sh`.
- [x] **M2.5 partial** — `JobRequest.secrets` (flat `dict[str, str]`);
      `GitDeployAdapter` materialises as env vars / `secrets.json` / `.env`
      per `params.secrets_format`; `JobWorker` purges values to `"***"` in
      DB on `finally` (no plaintext at rest).
- [x] **M6** — USB identity: multi-VID/PID per device, hub-port path,
      `device_leases` with atomic acquire (exclusive_device vs
      exclusive_hub), passive USB-ID learn during exclusive jobs, and
      `UsbFingerprintAdapter` for active depower/repower capture. REST:
      `/v1/devices/{id}/usb-ids`, `/v1/devices/lookup-by-usb`,
      `/v1/devices/{id}/learn-usb`, `/v1/leases`. HTMX list editor +
      Learn-USB button on the devices form. See **"USB-identity wiring"**
      section below for production hookup. 56 new tests across PR1–PR5.
      Important boundary: `/v1/leases` is ownership/exclusivity only —
      not a public usbip bind/attach API.
- [x] **M3.5** — MCU adapter chain, operator UI surface, bench-side
      provisioning. Done 2026-06-07. See the "M3.5 inventory" section
      below for what shipped and where.
- [x] 468 tests pass (non-`test_upnp.py`; that one has a pre-existing
      collection error tracked separately).

Not done:

- [ ] **M2 remainder** — GitHub OIDC verifier, policy file.
- [ ] **M2.5 remainder** — named secret profiles YAML (`bench-protomq` /
      `live-io-test` / `live-io-prod`); `${env:...}` server-side resolver;
      nested secrets.json values. Core materialisation (flat secrets, purge,
      env/json/dotenv formats) is done — see Done list.
- [ ] **M4** — per-family flashers (PicotoolFlasher, Uf2MscFlasher,
      BossacFlasher, optional DfuUtilFlasher), manual usbip bind/attach/
      detach UI buttons, ResetStrategy registry composing M3.5 primitives,
      solenoid-hub reset orchestration, OQ4 recover endpoint, OQ8
      hardcoded-password cleanup. This is also where the missing public
      USB-IP mutation surface still lives: today we have leases plus
      read-only busid inventory, but no public bind/unbind/attach/detach
      API. See `docs/ARCHITECTURE.md` §16 M4 for the full scope.
- [ ] **M5 remainder** — camera capture; artifact storage; Prometheus
      metrics; `raw-firmware-smoke` built-in; `live-io-test`/`live-io-prod`
      profiles; protobuf decoding for MQTT messages;
      `GET /v1/jobs/{id}/logs` non-blocking endpoint.
- [ ] HTMX dashboard (queue + device view).
- [ ] `topology/importers/` (`protomq_scripts.py`, `hardware_md.py`).
- [ ] we should make the website operator add the tinyuf2 target for new espressif chip based DUTs and any fallback target (for earlier versions of tinyuf2 before a board specific release). I guess we need a todos/status check on the website, so any missing info can be populated (including new pid/vid observations but remember duts are usb-hub and port based not vid/pid which changes depending on running app) and any recent status errors investigated. It's also worth noting the other boards (non esp) can have tinyuf2 bootloaders too, but rarely need updating (and use their own flasher tools / techniques as appropriate)

## M3.5 inventory (shipped 2026-06-07)

Eight commits closed M3.5 end-to-end. What landed, and where to look
first when picking up the next stage:

| Layer                                  | Module                                          | Tests | Commit  |
|----------------------------------------|-------------------------------------------------|-------|---------|
| FlasherProtocol + CliFlasher base      | `adapters/flashers/base.py`                     | 17    | 929c20f |
| EsptoolFlasher concrete                | `adapters/flashers/esptool.py`                  | 27    | 95ea02a |
| PioUploadFlasher + NoOpFlasher         | `adapters/flashers/{pio_upload,noop}.py`        | 14    | ff1bfd6 |
| TinyUF2 release fetcher                | `adapters/tinyuf2_fetcher.py`                   | 14    | d1af262 |
| SerialCaptureAdapter                   | `adapters/serial_capture.py`                    | 22    | 5ce3df3 |
| SolenoidHubAdapter + bench-side CLI    | `adapters/solenoid_hub.py` + `scripts/solenoid_hub_cli.py` | 15 | b9eceb4 |
| `/ui/leases` + exportable busids API   | router + templates + `api/hosts.py`             | 18    | 550f7c1 |
| TinyUf2Installer + bench-actions UI + serial-tail color | `adapters/tinyuf2_install.py` + form + router | 17    | 9e91197 |
| Discover-busids widget on device form  | `web/templates/devices_form.html` JS            | (covered) | 98237fb |
| `/ui/usbip` bench-wide overview + assign | `adapters/usbip_inventory.py` + router + templates | 18  | ee50bb3 |

**Operator can now (without SSH)**: see every busid across every host
at `/ui/usbip`; assign an unmatched busid to a device; power-cycle a
DUT via solenoid from its device form; install a TinyUF2 release
(fetched by board name with chip-family fallback, erase + flash
combined.bin at 0x0) via the same form; observe live serial as a
distinct green stream in the job log pane; see who holds every
device/hub lease at `/ui/leases` and force-release stuck ones.

**Operator still cannot (without SSH / internal code paths):** invoke a
public API to bind, unbind, attach, or detach a USB-IP device. The
controller has internal `UsbipBridge` support for controller-managed job
flows, but the public HTTP surface stops at lease ownership and
read-only exportable-busid discovery.

**Bench-side prereq** (operator-driven, one-time per host):
`sudo install -m 755 scripts/solenoid_hub_cli.py /opt/hil/solenoid_hub_cli.py`
on every host that physically owns an MCP23017. The
`setup-hil-host.sh` extension to fold this in is an M4 chore.

## Next stage plan

This is the plan for the next session. Pick a stage and work
top-down within it; the architecture-first → confirm → code cadence
still applies for any non-trivial design choice.

### Stage A — live bench validation (no new code)

Before writing M4 we should prove M3.5 holds against real hardware.
The Bench-actions UI exists but has only been exercised against
AsyncMock transports in tests. Operator-level checklist:

1. Deploy `scripts/solenoid_hub_cli.py` to `/opt/hil/` on
   `rpi-displays` (one-time `sudo install`).
2. Open `/ui/usbip`. Confirm every connected DUT shows up under
   `rpi-displays`, vid:pid matches expectations.
3. For each MCU DUT, open `/ui/devices/{id}/form` → click
   **Discover busids on hub host** → **Use this busid** → save the
   form. Confirms the discovery + assignment loop.
4. On a known-safe DUT (e.g. the RevTFT Feather already wired):
   - Click **Power-cycle (reset)** in Bench actions. Confirm the
     device re-enumerates in `dmesg` on rpi-displays.
   - Click **Install TinyUF2** with `board_name=feather_esp32s3_reverse_tft`
     and `chip=esp32s3`. Watch for the success panel. Confirm the
     DUT subsequently appears as a UF2 mass-storage device.
5. Submit a small arduino-ws job with `flash_mode=usbip` against the
   same DUT. Confirm the existing flow still works after the
   TinyUF2 bootloader change.
6. File any flake / unclear-error UX as concrete items for Stage B.

The model A re-enumeration warning from the per-phase exec-location
section still applies — esptool's reset can drop the usbip
attachment mid-write. M3.5 didn't fix that; it's an explicit M4
item ("UsbipBridge.attached() does not yet run the
vendor/usbip-autoattach reconciliation loop").

### Stage B — M4 flasher concretes

The Protocol + base class are already in place. M4's "drop in a new
family" is a single-file add per flasher. Order suggested by
operator priority:

1. **`PicotoolFlasher(CliFlasher)`** — RP2040 + RP2350. Inherit the
   three-stage BOOTSEL entry strategy from
   `vendor/hil-detection/scripts/pico_hil_flash.sh:259-320` (force
   reboot → 1200-baud stty sentinel → `machine.bootloader()` REPL
   fallback). Verbs: `probe()` (picotool info), `erase()`
   (range-based picotool erase with chip-type-aware defaults),
   `flash()` (picotool load --force + reboot -a), `reset(into=...)`.
2. **`Uf2MscFlasher(FlasherProtocol)`** — not a CliFlasher. Mount
   the BOOTSEL MSC drive (label from `device.uf2_msc.label` or
   default `RPI-RP2`/`RP2350`/`CIRCUITPY`), cp the `.uf2`, sync,
   unmount. Bootloader entry is NOT this adapter's job — operator
   either calls `PicotoolFlasher.reset(into="bootloader")` first
   (RP) or `SolenoidHubAdapter.samd51_uf2(channel)` (SAMD).
3. **`BossacFlasher(CliFlasher)`** — SAMD51 + SAMD21. `bossac
   --erase --write --verify --reset`. Always paired with the
   solenoid double-tap for bootloader entry on these boards.
4. **`DfuUtilFlasher(CliFlasher)`** — STM32 + nRF52. Planned but
   not on the critical path; ship when a real DUT requires it.

Each flasher gets the same `_locate()` PATH probe + sudo prefix
handling from `CliFlasher`. Each gets ~20 parser unit tests and
~8 verb tests (mirror EsptoolFlasher's 27).

### Stage C — manual usbip management buttons

The read-only `/ui/usbip` overview lands first; M4 extends it with
admin-gated mutation buttons:

Status on 2026-06-08: still not started. The implemented surface is
`GET /v1/hosts/{id}/usbip/exportable` plus `/ui/usbip` inventory /
assignment; no public mutation endpoint exists yet.

- Per-row **Bind / Unbind** buttons on unmatched busids (refuses to
  unbind a busid mapped to a device that holds a lease, with a
  force-override checkbox).
- Per-host **vhci status badge** on `/ui/hosts` and on the controller
  itself (parse `usbip port`, surface kernel ring-buffer errors).
- Per-host **Attached devices** panel for the controller showing
  what `usbip port` returns and **Detach** buttons.
- Per-device **Re-enum smoke test** button on `/ui/devices/{id}/form`
  that runs the handoff doc's validation sequence end-to-end
  (bind → attach → probe → probe → detach + unbind), captures
  stdout/stderr as a persistent artifact, behind an `exclusive_hub`
  lease the button acquires.
- If the consumer stops being "the controller host" and becomes a
  generic third-party app/machine, add a first-class export-session API
  rather than trying to overload `/v1/leases`. The lease should stay the
  ownership primitive; the export session should model transport state.

### Stage D — operator workflow features (task #17)

User-requested in the handoff edit above:

- A `/ui/todos` (or banner-on-dashboard) status page surfacing
  missing-info / recent errors. Specifically: devices missing
  TinyUF2 `board_name` + `fallback_board`; devices missing
  `solenoid_channel` / `hub_port_path`; recent job errors needing
  triage; passive USB-ID observations awaiting role assignment.
- Identity is **hub + port**, not VID:PID — VID:PID is an
  observation to log, not a key. This is a recurring source of
  confusion; the page should make the model legible.
- Non-ESP boards can also carry tinyuf2 bootloaders but rarely need
  updating; keep the form generic (no `chip=esp32*` lockout).

### Stage E — M4 deferred chores

These travel with M4 but are independent:

- **OQ4 recover endpoint** — `POST /v1/devices/{id}/recover`
  + `POST /v1/hosts/{id}/recover`. Admin-gated. Cancels in-flight
  job, force-releases leases, drives `SolenoidHubAdapter.all_off`,
  re-attaches usbip, re-probes. UI button.
- **OQ8 hardcoded-password cleanup** — see `vendor/README.md`
  hil-detection row. Two-PR plan:
  (a) upstream PR against `tyeth-ai-assisted/hil-detection`
  replacing the `RPI_PASSWORD` constant with a `HIL_BENCH_TOKEN`
  env lookup;
  (b) `scripts/setup-hil-detection-secret.sh` here that provisions
  `/etc/hil-detection/credentials.env` with a freshly-minted
  argon2id-hashed token on each bench. The plaintext never lands
  in this repo or DB.
- **`setup-hil-host.sh` extension** — install
  `scripts/solenoid_hub_cli.py` to `/opt/hil/` automatically;
  drop the Stage-A operator step.

### Stage F — broader CircuitPython matrix (post-M4)

When STM32 / nRF52 CircuitPython DUTs arrive, the
`DfuUtilFlasher(CliFlasher)` shell from Stage B drops in. The
`Uf2MscFlasher` already covers any non-ESP board that exposes a
UF2 bootloader (most CircuitPython boards do).

See `docs/ARCHITECTURE.md` §16 M3.5 / M4 for the full WipperSnapper
+ CircuitPython target matrix (STM32 / nRF52 are planned-future,
not dropped).
## Working with the user

The user is `tyeth@adafruit.com`, working at Adafruit, building
this for the WS-Python / ProtoMQ / display-test bench.

Conventions established over the session:

- **Architecture-first then code.** When asked "we'll need X", the
  right first move is updating `docs/ARCHITECTURE.md`, not writing
  Python. Confirmed by user: their first scoping pick was
  "Architecture doc first".
- **Ask focused questions before committing to a design.** The
  `AskUserQuestion` tool is the right vehicle — small numbers of
  options with the recommended one first. Multiple sessions of
  user direction shape have come from this. Don't bury decisions
  in implementation; surface them.
- **Bias for terse, complete-sentence updates** rather than running
  commentary. Match commit messages to the `docs:` / `ci:` /
  `vendor:` style already in `git log`.
- **No emojis.** Anywhere — replies, files, commit messages.
- **End every commit message with the** `https://claude.ai/code/
  session_01KXJbynVGheaSkFiZGxzSrU` **trailer** that's been
  consistent throughout the repo. The harness expects this format
  for the commit footer.
- **Don't create planning / decision / progress docs unless the
  user asks.** They did ask for this handoff explicitly — that's
  the exception, not the rule.

## Branch & push protocol

- Designated working branch: **`claude/protomq-hil-api-frontend-CUUTm`**
  (per the harness's task spec).
- Every push: `git push -u origin claude/protomq-hil-api-frontend-CUUTm`.
- Network retries: up to 4× with exponential backoff (2s, 4s, 8s, 16s).
- **Merges to `main` happen only when the user explicitly says so.**
  Their phrasing has been "merge that to main" / "get it into main".
  Use `git merge --no-ff` from a clean local `main` reset to
  `origin/main` so the merge commit matches their earlier PR-merge
  style.
- A **parallel session** has already pushed to `main` once during
  this work (commits `c673e14`, `bb81464`, `f35329d` — submodule
  pin bumps). Expect this to keep happening. Always
  `git fetch origin main` before any merge plan; if local and
  remote diverge, resolve via merge commit, not by force-pushing.
- The parallel session branch name was
  `claude/circuit-python-solenoid-api-tNrMy`. Different scope from
  this branch; ignore unless work overlaps.

## Submodule setup (easy to miss)

`.gitmodules` doesn't carry per-submodule remote config that we
actually need. After any fresh clone:

```bash
git submodule update --init --recursive
./scripts/setup-submodules.sh
```

The script is idempotent. It does two things:

1. **`vendor/protomq`** — sets *two* push URLs on `origin`
   (`tyeth-ai-assisted/protomq` and `tyeth/protomq`), so any push
   from inside that submodule reaches both forks. User explicitly
   asked for this: "always ensure it's pushed to the tyeth fork".
2. **`vendor/wippersnapper-arduino`** — adds an `upstream` remote
   pointing at `adafruit/Adafruit_Wippersnapper_Arduino` (fetch
   only; upstreaming goes via PR, not direct push).

Parallel-session submodule bumps will keep landing on `main`. When
integrating, run `git submodule update --init --recursive` after
the merge so the working tree matches the new pins.

## Bench topology, in one sentence per machine

Pulled from `vendor/hil-detection/references/hardware.md` and user
clarification:

- **Controller host** — independent machine running this repo's
  service. Not on the bench. User runs it locally at
  `~/dev-projects/python/usbip-hil-controller` under WSL for now.
- **`rpi-displays`** (`192.168.1.234`, Pi Zero 2W) — owns *all*
  microcontroller DUTs via the Genesys USB hub (`05e3:0610`) with
  MCP23017 solenoid power/reset at I²C `0x20`. All eight solenoid
  channels are now considered operational (OQ9 directive: "assume
  all solenoid channels are working"). Can run many concurrent
  jobs (per-device locks); `exclusive.host: true` serialises
  everything on it.
- **`rpi-hil001` … `rpi-hil007`** — each owns SBC DUTs. **One test
  or suite at a time per host** (`max_concurrent_jobs: 1` in §5.1).
  Per-port power control planned, not yet wired.
- **`pi5-protomq`** (`192.168.1.210`) — ProtoMQ broker (MQTT `1884`,
  web UI `5173`). The controller observes it, does not host it.
- Every HIL host: `pi` user with controller's SSH key already
  authorised. The hardcoded `RPI_PASSWORD` constant in
  `vendor/hil-detection/tests/conftest.py` (value deliberately not
  duplicated here — read it from the submodule if needed) is a residue
  flagged for cleanup PR (open question 8).

## Where to look first

- **`docs/ARCHITECTURE.md`** — full design. **§15 is now "Design
  decisions (formerly open questions)"** with all sixteen items
  resolved by stakeholder directive (verbatim quotes preserved).
  §16 has the milestone cut.
- **`vendor/hil-detection/references/hardware.md`** — the
  hand-maintained topology + solenoid map + USB mode tables. Direct
  input to the planned `hardware_md.py` importer.
- **`vendor/protomq/scripts/*.json`** — one demo per `(board,
  display)` pair. Source of truth for device↔display wiring.
  Direct input to the planned `protomq_scripts.py` importer.
- **`vendor/hil-detection/tests/`** — pytest fixtures already
  driving the bench over SSH. This is the *prototype* of the
  controller's adapter layer, not something to replace.
- **`vendor/wippersnapper-arduino/src/provisioning/ConfigJson.cpp`**
  — confirms `io_url` (string) / `io_port` (int) as the broker
  override fields. The example secrets file at
  `examples/wippersnapper-arduino/secrets.example.json` is wired
  to this contract.
- **`.github/workflows/ws-python-ci.yml`** — the recently-landed
  reusable workflow. Default tests filter is `-m eink_large`;
  default controller URL is `http://wan.gdenu.fi:8080`.

## Open asks from the user

`scripts/mint-token.py` is implemented (M2 partial). It accepts
`--db`, `--label`, `--pool`, `--repo`, writes an argon2id hash
row, and prints the plain `hil_<id>_<secret>` token once.

Default controller URL confirmed: `http://wan.gdenu.fi:8080`.

All sixteen original open questions are now resolved. See §15 of
`docs/ARCHITECTURE.md` for the verbatim stakeholder directives.
The resolutions added the following **new implementation tasks**
that the previous M0–M4.5 work did not yet cover:

- **OQ2 / OQ5 (camera pipeline).** Replace the per-host
  `copy_from` sketch with a central streaming pipeline. Aux
  records gain a `roi`; job event log records
  `(start_ts, end_ts, roi)`; pre-roll + trailing-buffer duty
  cycle.
- **OQ4 (force recover).** Add `POST /v1/devices/{id}/recover`
  and `POST /v1/hosts/{id}/recover`, admin-gated. Cancels
  in-flight, clears locks, clean detach + power cycle, re-probe.
- **OQ7 (drift detectors).** `protomq_scripts.py` and
  `hardware_md.py` importers under
  `src/hil_controller/topology/importers/` — flag-only, never
  overwrite `/etc/hil/topology.yaml`.
- **OQ11 (HTTP agent transport).** Add `src/hil_controller/
  hosts/agent.py` as the *preferred* transport — HTTPS, mTLS or
  controller-signed token. SSH stays as fallback. Per-host
  config picks. The Protocol in `hosts/base.py` already supports
  this; just add the implementation.
- **OQ12 (artifact transfer fallback).** Per-host
  `fetch_locally: bool` config. Default `false` keeps controller-
  pulls-then-pushes; opt-in lets specific hosts fetch directly.
- **OQ15 (forensic snapshots + retention daemon).** Snapshot
  every permissive-script payload to
  `/var/lib/hil/forensic/<job-id>/`. Background sweep deletes
  on the *earlier* of 30 days OR `/var/lib/hil` > 75% capacity.

These are not on the "do not re-litigate" list — they're now
concrete work items. Order of priority (stakeholder hasn't
sequenced these yet, so this is a suggestion): OQ11 first
(unblocks restricted-network HIL hosts), OQ4 next (operational
necessity once real DUTs land), then OQ2/OQ5 (M5 territory),
then OQ7 and OQ15.

## Decisions already made — don't re-litigate

These came up in conversation and were settled. Don't open them
again unless the user asks:

- **Stack**: FastAPI + HTMX/Jinja. Not Flask, not Node.
- **Queue**: in-process asyncio + SQLite. Not Redis/Celery.
- **Auth**: per-repo bearer tokens **and** GitHub Actions OIDC.
  Both, not either-or.
- **Controller location**: independent host, not on the bench.
- **Host transport**: dual SSH + HTTP-agent. SSH already
  shipped; the agent is now the *preferred* path per stakeholder
  directive on OQ11 — see "Open asks" above. `HostTransport`
  Protocol already abstracts both.
- **SBC concurrency**: 1 per host, period. MCU host: unbounded,
  per-device locks only.
- **SBC job shape**: `payload.kind = "git-source"` + `GitDeploy`
  adapter fills the flasher slot in the state machine. Not a
  separate deploy phase.
- **Secret profiles**: a named-bundle abstraction (§5.8) — three
  preset profiles (`bench-protomq`, `live-io-test`, `live-io-prod`)
  rendered into `secrets.json` / `.env` per job.
- **Trusted firmware**: gated by a `trusted-firmware` capability
  in the auth policy, plus two permissive built-in scripts
  (`raw-firmware-smoke`, `git-clone-and-run`).
- **Default WS-Python test filter**: `-m eink_large` only, for now.
  WS-Python repo will add the marker when ready.
- **Default controller URL**: `http://wan.gdenu.fi:8080`. Wired as
  the default in `ws-python-ci.yml`.

## Things NOT to do

- Don't write code yet unless the user says so. The pattern has
  been doc → user confirms → doc some more → user confirms → code.
- Don't add a Python Wippersnapper submodule. It's private /
  unreleased / the sandbox can't see it. Open question 10.
- Don't try to fix the hardcoded password in `vendor/hil-detection/
  tests/conftest.py` from inside this repo. It's a separate PR
  against `hil-detection` (open question 8); flag it, don't
  hot-fix.
- Don't force-push to the feature branch (or to main, obviously).
  The user reviews via the GitHub UI.
- Don't broaden the `-m eink_large` default until the user
  explicitly says the rest of the bench is wired up.

## USB-identity wiring (M6)

The full M6 design lives in `docs/ARCHITECTURE.md` section 16. This is
the operator's checklist for going from "all tests green" to "the
Learn-USB button actually depowers a hub port and captures a real
VID/PID."

**Topology YAML — add the hub-port fields to every MCU device:**

```yaml
- id: mcu-pyportal
  host_id: rpi-displays
  hub_host_id: rpi-displays          # defaults to host_id; usbip server
  hub_port_path: "1-1.1.3"            # sysfs bus-id — the real identity
  solenoid_channel: 3                 # MCP23017 channel (0..7)
  usb_serial: "F1DF00AE..."           # iSerial, for matching across resets
  usb_ids:
    - { vid: "239a", pid: "8053", role: runtime,    description: "WipperSnapper" }
    - { vid: "239a", pid: "8054", role: runtime,    description: "CircuitPython" }
    - { vid: "239a", pid: "0035", role: bootloader, description: "UF2" }
```

Roles are mechanism-level (`runtime | bootloader | dfu | msc | cdc |
unknown`); product info goes in `description`. The legacy single
`usb: {vid, pid}` block is still accepted and seeds one `unknown` row.

**Migration** is automatic. On first boot after upgrade:
- `ALTER TABLE` adds the four new device columns.
- Any pre-existing `usb_json` is backfilled into `device_usb_ids` with
  `source='migration'`, `role='unknown'`.
- `device_leases` table is created, then `startup_sweep` releases any
  active lease whose `job_id` is no longer in an active state (recovers
  from a crashed controller without manual cleanup).

**Wiring the active learn flow** (one-time, in your deployment entry
point — e.g. `main.py` or a startup hook):

```python
from hil_controller.adapters.usb_fingerprint import UsbFingerprintAdapter
from hil_controller.adapters.usb_scan import make_ssh_scan_fn

def usb_fingerprint_provider(*, db_path: str) -> UsbFingerprintAdapter:
    # 1. Build a transport for the hub host (your SSHTransport / similar).
    # 2. Wrap vendor/hil-detection/usb_hub.py's SolenoidHubController in an
    #    async facade (all_off / port_on / port_off).
    hub = AsyncSolenoidHub(transport=hub_transport)
    return UsbFingerprintAdapter(
        db_path=db_path,
        hub=hub,
        scan_fn=lambda: ssh_scan(hub_transport),  # parses `usbip list -l`
    )

app.state.usb_fingerprint_provider = usb_fingerprint_provider
```

Without the provider, `/v1/devices/{id}/learn-usb` and the UI button
still run end-to-end (lease acquired, DB upserted) but exercise no-op
placeholders for the hub and the scan — useful for testing the flow
but it captures nothing real.

**Passive learn** needs no wiring: as long as the adapter the host
registry returns for a job exposes a `transport` attribute with a
`run(cmd)` coroutine, `Scheduler._maybe_start_passive_learn` will
spawn the polling loop automatically. `SSHTransport` already qualifies.

**Knobs:**
- `UsbFingerprintAdapter(settle_s=2.0, reset_settle_s=1.5)` — adjust
  for slow-enumerating boards. SAMD51 double-tap timing parameters
  go on the `hub.port_off` call directly (see `vendor/hil-detection/
  usb_hub.py:68` for the defaults).
- `passive_learn_loop(interval_s=3.0)` — bump down if you want
  faster reaction to VID/PID flips during a job, up if SSH cost is
  noticeable.

**REST endpoints summary:**

```
GET    /v1/devices/{id}/usb-ids               # list
POST   /v1/devices/{id}/usb-ids               # manual add
DELETE /v1/devices/{id}/usb-ids/{row_id}      # remove
POST   /v1/devices/lookup-by-usb              # {vid,pid,iserial?} -> [devices]
POST   /v1/devices/{id}/learn-usb             # {include_reset_cycle?}
GET    /v1/leases?active_only=true            # observe exclusivity
POST   /v1/leases                             # manual claim (rarely needed)
DELETE /v1/leases/{id}                        # force-release
```

What is still missing for "let a third-party app use this device's USB
over the network":

- Public bind/unbind routes for the USB-server host.
- Public attach/detach routes for the consuming host.
- A durable export-session record separate from `device_leases`.
- Revocation / reclaim semantics beyond the still-planned recover flow.

**Operator gotcha — exclusive_hub during learn is loud.** A learn-USB
pass briefly depowers *every* port on the target hub, so any other job
sharing that hub will see its DUT vanish. The lease primitive prevents
two such operations colliding, but it does not pause concurrent normal
jobs — schedule learn passes when the hub is idle, or accept the blip.

## Per-phase execution-location for arduino-ws jobs (M7)

WipperSnapper arduino-ws jobs used to run **every phase on the DUT's
host**. rpi-displays (the DUT host) has only 415 MB RAM and a 208 MB
tmpfs `/tmp`, so a PlatformIO build there OOM-thrashes / runs out of
disk. The controller (Tachyon, 192.168.1.169) builds easily. So each
  phase's **execution host** is now selectable.

**Carrier:** `params.exec` (pass-through dict, mirrors `params.protomq`):

```
params.exec = {
  "build_host":   "controller" | "dut-host",          # where `pio run` compiles
  "flash_mode":   "usbip" | "ship-artifacts",          # how firmware reaches the DUT
  "test_host":    "controller" | "dut-host" | "none",
  "protomq_host": "controller" | "dut-host" | "off",
  "pio_env":      "<platformio env>",
}
```

Near-term defaults (set by the arduino-ws form builder): build +
protomq on the controller, `flash_mode=usbip`, pytest none. Under this
layout the DUT's `MQTT_HOST` = the controller LAN IP
(`config.controller_ip` / `HIL_CONTROLLER_IP`, default 192.168.1.169),
not 127.0.0.1.

**Code map:**
- `adapters/usbip_bridge.py` — `UsbipBridge` brokers a device from its
  USB-server host onto a client. `attached()` async CM does ensure-vhci →
  bind (server) → attach (client) → yield the new `/dev/tty*` → detach +
  unbind in a `finally`. Pure parsers `parse_usbip_port` /
  `diff_serial_ports` are unit-tested.
- `adapters/arduino_ws_exec.py` — `ArduinoWsExecAdapter` holds two
  transports (controller + DUT-host), delegates clone/build/run to an
  inner `GitDeployAdapter` on the build host, and adds **flash** as a
  distinct phase (usbip upload on the controller, or ship-artifacts +
  esptool on the DUT). usbip flash is wrapped in an `exclusive_device`
  lease released in a `finally`. Cross-host build+run → `NotImplementedError`.
- `hosts/registry.py` `make_adapter` (DB-free, unit-tested) routes jobs
  with `params.exec` to the new adapter, building the DUT transport from
  the device's `hub_host_id`.
- Topology: device `host_id` = execution host (the controller), separate
  from `hub_host_id` + `hub_port_path` (where USB physically lives). See
  `mcu-feather-esp32s3-revtft` in `deploy/topology.example.yaml`.
- `scripts/setup-hil-host.sh` provisions passwordless-sudo usbip +
  vhci-hcd/usbip-host modules + usbipd.

**⚠ usbipd MUST be running on the USB-server host (the one physically
holding the DUT, e.g. rpi-displays).** It is the daemon the controller's
`usbip attach` connects to on TCP **:3240**. If it is down, the flash phase
fails with `usbip attach failed (exit 1): usbip: error: tcp connect` — the
build can succeed and you still never reach the DUT. Diagnose + start:

```
# on the USB-server host (rpi-displays):
ss -ltn | grep 3240                              # is it listening?
systemctl is-active hil-usbipd                   # service up?
sudo -n /usr/sbin/usbip list -l | grep 1-1.1.1.4 # revtft Feather busid 239a:8123
```

For persistence, `setup-hil-host.sh` enables a packaged `usbipd.service` when
present, else installs a `hil-usbipd.service` unit (Debian/RPi ship the
`usbipd` binary but no unit). **rpi-displays was provisioned 2026-06-07**: the
`hil-usbipd.service` is now enabled + active, `usbip-host` and `vhci-hcd`
modules persist via `/etc/modules-load.d/hil-usbip.conf`, the
`/etc/sudoers.d/hil-usbip` drop-in is in place, and a bind→list-r→unbind cycle
against `1-1.1.1.4` (RevTFT Feather, `239a:8123`) was confirmed working. The
controller (client) side needs only `vhci_hcd` loaded; the bridge `modprobe`s
it at flash time. busid for the revtft Feather = `1-1.1.1.4`. Note that
`/usr/sbin/usbip` is not on `pi`'s PATH — use the absolute path (which is what
the sudoers drop-in permits anyway).

**⚠ Model A re-enumeration risk — VALIDATE ON HARDWARE BEFORE TRUSTING.**
The ESP32-S3 re-enumerates (ROM↔app) during flash, which can drop the
one-shot usbip attachment mid-upload. `UsbipBridge.attached()` does **not**
yet run the `vendor/usbip-autoattach` reconciliation loop that handles
re-enum. So before relying on `flash_mode=usbip` for the revtft Feather,
run the **cheap validation** (no long build): on rpi-displays
`sudo usbip bind -b 1-1.1.1.4`; on the controller `sudo modprobe vhci-hcd`
+ `sudo usbip attach -r 192.168.1.234 -b 1-1.1.1.4`; then
`esptool chip-id` and a second reset-crossing call (`esptool read-mac`)
to exercise two re-enum cycles. If the attachment survives, usbip is
viable; **if it flakes, switch the job to `flash_mode=ship-artifacts`**
(already implemented) rather than grinding on usbip. These are privileged
commands on production hosts — run them deliberately, not from an agent
session.

## 2026-06-12 bench note: QT Py ESP32-S3 on channel 4

Live bench validation on 2026-06-12 showed that the currently-toggled
device on `rpi-displays` **channel 4** is the QT Py ESP32-S3 (4MB flash,
2MB PSRAM), despite older notes mapping channel 4 to the PyPortal
Titano. The reliable identification path was:

- runtime: `/dev/serial/by-id/usb-Adafruit_QT_Py_ESP32-S3__4MB_Flash_2MB_PS-if00`
- bootloader after `sudo stty -F /dev/ttyACM0 1200`:
  `/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_F4:12:FA:5A:35:B4-if00`

Validated flows:

- public release:
  `wippersnapper.qtpy_esp32s3_n4r2.fatfs.1.0.0-beta.129.combined.bin`
- PR artifact:
  `adafruit/Adafruit_Wippersnapper_Arduino#927`
  (`wippersnapper.qtpy_esp32s3_n4r2.fatfs.1.0.0-adafruit-80b261f1.zip`)

Observed fixed PR 927 behavior: invalid pixel write/delete requests log
handled errors (`ERROR: Pixel strand not found...`, `ERROR: Strand not
found...`) while MQTT pings continue, instead of crashing/rebooting the
board.

## Session lineage

This handoff covers the work done in session
`session_01KXJbynVGheaSkFiZGxzSrU`. If you're picking up after a
context compaction within the same session, the conversation
summary should still cover the recent message; this doc is the
durable record. If you're a fresh agent on a new session, this is
the file to start from after `docs/ARCHITECTURE.md`.
