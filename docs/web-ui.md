# Web UI — operator pages and their API equivalents

The web UI (`/ui/`, cookie login with the same bearer token) is meant to give a
human the same view and the same levers an agent gets from the HTTP API, the MCP
server and the skills. Every page below names the endpoint that returns the same
data, so a human and an agent can check each other's work.

| Page | What a human sees / does | API / CLI equivalent |
|---|---|---|
| **Dashboard** `/ui/` | hosts, devices, cameras, recent + active jobs | `GET /v1/targets`, `GET /v1/jobs/{id}` |
| **Jobs** `/ui/jobs` | job list; new-job forms (Python, Arduino, WS, firmware-bench, bisect) | `POST /v1/jobs` |
| ↳ **+ Raw JSON** `/ui/jobs/raw` | paste a `POST /v1/jobs` body *or* an MCP agent job; Validate shows the exact request (credentials masked) and which device it would get; broker selector (default / staging / dev-lan) for `io_*` stages; Submit goes through the same handler as `POST /v1/jobs` | `POST /v1/jobs`, MCP `prepare_job` |
| ↳ **Recipes** `/ui/jobs/profiles` | job recipes, brokers, credential guidance (names only), AgentJob schema; "Use this" pre-fills Raw JSON | `GET /v1/job-profiles`, MCP `job_guide` |
| ↳ **Which device?** `/ui/jobs/resolve` | dry run: the scheduler's pick for a target (pool/kind/model/caps/strand requirements), why, and every rejected candidate | `POST /v1/topology/resolve` + scheduler matching |
| ↳ job detail `/ui/jobs/{id}` | state/result, device + **host**, script/pool/secrets profile, start/finish, the submitted request (credentials masked), assets, live log with per-stream filters (stdout/stderr/serial/protomq/state/metric/artifact) and text search | `GET /v1/jobs/{id}`, `/wait`, `/assets` |
| **Hosts** `/ui/hosts` | transport, address, bridge API + token-variable state, last seen, hardware, load; add/edit (incl. `esp-usbip-bridge` transport, Bridge API URL, Token env var) | `GET /v1/hosts` |
| ↳ host detail `/ui/hosts/{id}` | config, hardware, every device that names the host as owner or USB hub (wiring, outage reason), cameras, active leases, recent jobs | `GET /v1/hosts/{id}` |
| **Devices** `/ui/devices` | per device: hub host · port · solenoid/BOOTSEL · power control · iSerial, flasher/build target, outage kind/reason/since/attempts/next retry/last probe; **Retry all temporary**; **Who owns this USB device?** | `GET /v1/devices`, `POST /v1/devices/availability/retry`, `POST /v1/devices/lookup-by-usb` |
| ↳ device form | status `available`/`unavailable` + outage kind + reason, Power control (`bridge-port`, `bridge-port:force`), USB IDs learn/add, camera ROI; Bench actions incl. **Rescue…** link | `PATCH /v1/devices/{id}` (partial update), `GET/POST /v1/devices/{id}/usb-ids`, `/learn-usb` |
| ↳ **Rescue** `/ui/rescue/{device}` | board rescue, non-destructive first: **Probe serial** (poke picker: enter/ctrl-C/ctrl-D/hex, capture window, optional expect regex), **Enter BOOTSEL** (RP2040/RP2350 solenoid press + power-cycle), **Picotool** (info / reboot-BOOTSEL / load / erase / reboot, firmware upload, BOOTSEL fallback), **Esptool** (chip_id / read_mac / erase_flash / write @ address, upload). Each button submits a **bare** firmware-bench job (`params.bare: true`, no protoMQ) and redirects to the job page; probe output lands as the `serial-probe.log` asset | `POST /v1/jobs` (`script: firmware-bench`, rescue stages — see `docs/api.md` “Board rescue”) |
| **Leases** `/ui/leases` | active leases (live), history toggle, **Hold a device or hub** form, force release | `GET/POST/DELETE /v1/leases` |
| **USB-IP** `/ui/usbip` | every USB/IP server's busids matched to devices, USB descriptors, hub port status, `/dev` symlinks; solenoid channels; for **esp-usbip-bridge** hosts the bridge `/api/info`, token state and On/Off/Cycle per hub port (empty/switched-off ports too) | `GET /v1/hosts/{id}/usbip/exportable` |
| **Hardware** `/ui/hardware` | auxes (interface, streams, `bridge:<host>` muxes, connections), peripherals, I2C strands with every component's address / TCA channel / capabilities / WS types and routes | `GET /v1/aux`, `GET /v1/strands` |
| **Topology** `/ui/topology` | what a restart would do: rows whose UI edits it **reverts** (field by field), DB-only rows, deleted rows it re-creates; **Preview save** (change log + diff) and **Save to topology file** (in place, comments kept, backup); export download | `GET /v1/topology/drift`, `POST /v1/topology/save[?dry_run=true]`, `GET /v1/topology/export` |
| **Twins** `/ui/twins` | physical inventory: twins, identities, ports, connections, slot history, observations; paste/upload → Preview → Apply; **Collect from bridge**; Linux collector command per host; close connections | `/v1/twins/*`, `hil-twins-import`, `python -m hil_controller.twin_collectors` |
| **Cameras** `/ui/cameras` | cameras, previews, per-device ROI calibration | `/v1/cameras`, `/v1/devices/{id}/camera/*` |
| **Scripts** / **Assets** | protoMQ scripts; job assets with purge | `GET /v1/jobs/{id}/assets` |
| **Skills** `/ui/skills` | the same skills the MCP server serves agents, read-only | MCP `list_skills` / `read_skill` |
| **Audit** `/ui/audit` | audit log (renames, seeder drift, auth failures — token hints masked) with event/entity filters | — |

## Edits are live — but a restart re-applies the topology YAML

Hosts, devices and strands created or edited in the UI (or via `/v1/strands`)
are mirrored into the scheduler's registry immediately: a UI-added
esp-usbip-bridge host gets its transport, a UI-added device can take jobs, a
strand route is honoured, all without a restart. Any device status other than
`available` keeps jobs off the device.

On every start the seeder re-applies `topology.yaml` (`HIL_TOPOLOGY_FILE`,
default `/etc/hil/topology.yaml`; a missing file means a DB-only bench). Rows the
YAML defines are overwritten with its values — except device runtime wiring
(hub host/port, solenoid/BOOTSEL, iSerial, serial port), which the DB keeps —
and rows only in the DB survive. The **Topology** page (and a banner on Hosts
and Devices) lists exactly which UI changes a restart would revert. **Save to
topology file** (`POST /v1/topology/save`) writes the live DB back into the seed
file in place: only differing fields change, DB-only rows are appended, rows
deleted in the UI are removed, comments/order/unmanaged keys are kept, and the old
file is kept as `<file>.bak-<timestamp>`. Committing the file to git is a separate,
later step (bench policy: commit + push so git matches the bench).

## Security notes

- The UI never stores or shows secret values. Bridge tokens are referenced by
  environment-variable name (`token_env`, e.g. `HIL_BRIDGE_TOKEN_S31` in
  `run/controller.env`); a value pasted into that field is rejected. Pages show
  only whether the variable is set.
- Job requests are shown with credential values masked to their last four
  characters (`redact.py` policy); env-var and profile names stay readable.
- Twins pages require `inventory:read` (write for apply/collect/close) and
  all-pools access, exactly like `/v1/twins`.
