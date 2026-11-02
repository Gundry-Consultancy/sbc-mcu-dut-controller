---
name: hil-inventory-twins
description: "Inventory every physical asset on the HIL bench — SBC hosts, MCU DUTs, USB/IP bridges (S31 esp-usbip-bridge), USB-UART bridges/debug probes, I2C strand sensors, cameras — into the physical digital-twins DB, and keep the controller's logical assets (hosts, devices, USB IDs, busids, strands, topology YAML) in sync with what is really plugged in. Covers the two equal paths (web UI pages vs HTTP API/CLI), the collectors (Linux sysfs, S31 bridge), preview → review → apply, slot binding and replacement with compare-and-swap, matching USB endpoints to devices, and finishing with a topology drift check + export so a restart does not revert anything. Use when: populating or auditing the twins DB, a board/sensor was swapped or moved, a device's USB IDs/serials/busid are wrong or missing, /ui/topology shows drift, or you need to know which physical unit sits in a logical slot. NOT for: running test jobs (hil-job-api), bench power/availability triage (hil-bench-recovery), strand routing during a job (hil-i2c-strands)."
---

# hil-inventory-twins

Two inventories, kept deliberately separate:

| | Logical assets (scheduler) | Physical twins (inventory) |
|---|---|---|
| What | `hosts`, `devices`, `auxes`, `strands`, `cameras` rows — **slots** jobs are scheduled onto | `physical_twins` — one opaque UUID per real specimen, with identities, ports, connections, slot history |
| Source of truth | live DB; `topology.yaml` re-seeds it on every restart | live DB only (the seeder never touches twins) |
| Human UI | Hosts, Devices, USB-IP, Hardware, Topology pages | **Twins** page (`/ui/twins`) |
| Agent API | `/v1/hosts`, `/v1/devices`, `/v1/strands`, `/v1/topology/*` | `/v1/twins/*`, `hil-twins-import`, `python -m hil_controller.twin_collectors` |

A slot (e.g. `devices.id = mcu-qtpy-esp32s3-n4r2-hil006`) outlives the board in
it. Swapping the board = a **new twin** assigned to the same slot; the old twin
and its history stay queryable. Full model and rules: `docs/digital-twins.md`,
`docs/twin-collectors.md`, `docs/twin-discovery.md`.

Everything below can be done by a human in the web UI **or** by an agent over the
API — the pages show the same data the endpoints return. Pick whichever the
operator is using; name the page when you report back so they can check it.

## Ground rules (never break these)

- **Never fabricate identity.** No serial from an address, VID/PID, MAC, hostname
  or bus path. Unknown is `identity_status: unknown|not_exposed|read_blocked`.
- **A USB endpoint is not a board.** Collector rows are `kind: usb_endpoint`,
  `physical_device_association: unverified`. A CP2102N/CH9102/debug-probe serial
  belongs to *that bridge chip*, not the MCU behind it. Two endpoints can be one MCU.
- **`unique: true` needs `confidence: verified`** with evidence. A Raspberry Pi
  64-bit board serial is verified; most USB iSerials are reported, non-unique.
- **Always preview before apply.** The UI forces this; over the API call
  `POST /v1/twins/observations/preview` first.
- **Replacements are explicit.** A slot binding always carries `expected_twin_id`
  (mandatory compare-and-swap): the current occupant's twin ID when replacing,
  `null` to assert the slot is empty. A 409 means the slot holds something else:
  stop and look before retrying.
- **Tokens by name only.** Bridge tokens live in `run/controller.env`; hosts store
  `token_env` (e.g. `HIL_BRIDGE_TOKEN_S31`). Never paste a token into a form,
  YAML, an observation, or a commit.
- **Read-only discovery.** Collectors never power-cycle, bind usbip, open serial
  ports or change mux routes. Sensor identity reads need the strand routed and a
  lease held first (`docs/twin-discovery.md`).

## Workflow

### 0. Snapshot the starting state

| Human (web UI) | Agent (API) |
|---|---|
| **Topology** page — note the drift table is empty (or what is pending) | `GET /v1/topology/drift` |
| **Twins** page — current twins, unassigned slots (`Twins → Slots`) | `GET /v1/twins`, `GET /v1/twins/slots?kind=device` |
| **USB-IP** page — every host's busids + matched device | `GET /v1/hosts/{id}/usbip/exportable` per usbip-server host |

If drift is non-empty, resolve it first (step 6) so you are not inventorying
against stale slots.

### 1. Make sure every host exists and is reachable

- SBC/Pi hosts: `transport: ssh`. Controller itself: `transport: local`.
- **S31 / esp-usbip-bridge — discover, review, apply.** Twins → **Bridges**
  (`/ui/twins/bridges`) or the API:

  | Human (web UI) | Agent (API) |
  |---|---|
  | **Discover (mDNS)** — browses `_esp-usbip._tcp` (+ the `_esp-usbip-bridge` subtype of `_usbip._tcp`), confirms each answer with `GET /api/capabilities` | `GET /v1/bridges/discover?timeout_s=3` |
  | **Add by address** — same check for one IP / `name.local` (use this across Tailscale, and for pre-1.0 bridges, which do not advertise) | `POST /v1/bridges/probe {"address": "192.168.1.133"}` |
  | **Apply: create host** on a `new` proposal (edit id / role / tags / token env var *name*) | `POST /v1/bridges/apply {"address", "host_id"?, "token_env"?}` |

  Proposals are `new` / `known` / `moved` / `unconfirmed`; matching is by base
  MAC (from cached capabilities), then hostname, then address, and one bridge
  seen on Ethernet + Wi-Fi is one proposal. New hosts get the mDNS name
  `<hostname>.local` as address (the IP is shown for information) so DHCP
  leases don't matter; a name-addressed host is only `moved` when the name stops
  resolving or answers with another base MAC (`move_reason`). A bridge that
  renamed itself (same MAC; e.g. `usbip-d19bee` -> `usbip-s31-d19bee`) is
  `known` with `renamed_from`; edit the host's address to the new `.local` name
  by hand if the note says it still uses the old one. **Nothing is ever added, removed or
  disabled automatically** — a bridge missing from a browse is just listed as
  not seen (LAN flaps, multicast does not cross Tailscale). A `moved` bridge is
  fixed by editing the host, by hand. mDNS needs the `[discovery]` extra
  (python zeroconf) or `avahi-browse` on the controller; without either, use
  add-by-address. Manual Hosts → Add Host (Transport `esp-usbip-bridge`,
  Address, optional Bridge API URL, Token env var name) still works.
- The host detail page (`/ui/hosts/<id>`) shows whether the token variable is
  set and the bridge's capabilities (api_version, fw, board, MAC, features —
  `pre-1.0` when it has no `/api/capabilities`; **Refresh capabilities**).
  Agents: `GET /v1/bridges/{id}/capabilities`.
- A UI-added host gets a transport immediately (no restart). Check the host
  detail page shows no **no transport** badge, then open its USB-IP section: a
  bridge shows its `/api/info`, devices, hubs and per-port power buttons.

### 2. Capture observations

| Asset | Human | Agent / CLI |
|---|---|---|
| S31 bridge USB endpoints | Twins page → **Collect from bridge** (lands on preview, never auto-applies) | `python -m hil_controller.twin_collectors --host-id usbip-bridge-s31 --scan-id inv-<date>-a --output obs.json bridge --url http://usbip-xxxxxx.local --token-env HIL_BRIDGE_TOKEN_S31` |
| Linux host board + its USB endpoints | Twins page shows the exact command per Linux host — run it on that host (needs its sysfs) | `python -m hil_controller.twin_collectors --host-id rpi-hil006 --scan-id inv-<date>-b --output obs.json linux` (add `--bind-host-slot --expected-board-serial <serial>` to bind the host slot) |
| I2C strand sensors (SGP3x/4x, HDC302x, STCC4, TMP119) | route + lease the strand first, then upload the reader's observation | `hil_controller.twin_sensor_readers.read_sensor_identity` (exact model + address + verified route/lease) |
| Things with no readable serial (cameras, breakouts, hubs) | write an observation with an `inventory_tag` (operator asset label) | same JSON via preview/apply |

Use a **fresh `--scan-id` per capture**. Re-importing the same JSON is
idempotent; reusing a scan ID with different content is a 409 by design.

### 3. Preview and review

- Human: Twins → paste/upload the JSON (or arrive from Collect) → **Preview**.
  Each entry shows action (create / update / replay), candidate existing twins
  matched by *verified unique* identity, and any conflict with its status/detail.
  **Use this twin** pins an observation to a matching unit and re-previews.
- Agent: `POST /v1/twins/observations/preview` per observation (or
  `hil-twins-import --db run/jobs.db --input obs.json` = dry run on a temp copy).

Decide per entry: new specimen, update of a known twin (set `twin_id`), or skip.
Endpoint rows with only reported iSerials will create *separate* records on
every scan unless you map them to an existing `twin_id` — do that mapping.

### 4. Apply

- Human: **Apply** on the preview page (all-or-nothing, re-validated).
- Agent: `POST /v1/twins/observations`, or
  `hil-twins-import --db run/jobs.db --input obs.json --apply --backup run/jobs.db.pre-twins-<date>`
  (a backup path is mandatory when applying).

### 5. Bind slots and wiring

- Slot binding (twin ↔ `devices`/`hosts`/`strand_components`/`peripherals`/
  `auxes`/`cameras` row) is part of an observation's `slot` with
  `expected_twin_id` for replacements. Check Twins → Slots afterwards: no
  physical board should be unbound, no slot should point at a removed board.
- Wiring between twins (MCU USB port → hub port, sensor → strand mux channel,
  debug probe SWD → target) is a twin **connection**; close stale ones from the
  twin detail page (**Close**) or `POST /v1/twins/connections/{id}/close`.

### 6. Sync the logical assets to what you found

Fix the scheduler's view so it matches the physical inventory:

| Mismatch you found | Fix (UI) | Fix (API) |
|---|---|---|
| Board is on a different port | USB-IP page → **Assign** the busid to the device (or edit Hub port path) | edit device / `usbip` assign |
| Device has no/old USB IDs | Device form → USB IDs → **Learn** | `POST /v1/devices/{id}/learn-usb` |
| Unknown USB device on a bus | Devices → **Who owns this USB device?** (searches learned IDs, iSerials, twin identities) | `POST /v1/devices/lookup-by-usb` |
| Board removed | Device form → Status `unavailable`, Outage kind `permanent`, reason (who/when) | `PATCH /v1/devices/{id}` `{"status":"unavailable","unavailable_kind":"permanent","unavailable_reason":"…"}` |
| New board/slot | Devices → Add Device (hub host = bridge for S31 DUTs, Power control `bridge-port` only behind a per-port switched hub; none on a root port) | — (UI or topology YAML) |
| New bridge found | Twins → Bridges → Apply on its proposal | `POST /v1/bridges/apply` |
| Strand mux found (`_i2c-strand._tcp`) | Twins → Bridges → strand-mux table: **link** a bridge's mux (aux `bridge:<host>`, matched by MAC — apply the bridge first) or add a mux box aux; then point the strand's mux at it on Hardware → Edit strand | `POST /v1/bridges/strand-muxes/apply`, then `PUT /v1/strands/{id}` |
| Sensor moved strand / channel | Hardware → Edit strand | `PUT /v1/strands/{id}` |

Agents can edit a device without the form: `PATCH /v1/devices/{id}` with just
the fields to change (e.g. `{"model": "pi4"}`).

UI/API edits are live for scheduling immediately. **But a restart re-applies
`topology.yaml`**: finish on the **Topology** page — **Preview save**, check the
diff, then **Save to topology file** (or `POST /v1/topology/save?dry_run=true`,
then without `dry_run`). It updates the seed file in place (comments kept, backup
written) and returns `drift_after` — all zeros means a restart now re-seeds
exactly the live DB. Commit the file to the deploy repo separately (bench policy:
commit + push so git == bench).

### 7. Report

Report per host: twins created/updated, slots (re)bound, replacements (old → new
twin), unresolved endpoints that still need a human mapping, logical fixes made,
and the topology commit. Link the pages (`/ui/twins/<id>`, `/ui/hosts/<id>`,
`/ui/topology`) so the operator can verify by eye.

## Gotchas

- `/ui/twins` needs `inventory:read` (write for apply/collect/close) **and**
  all-pools; a partial-pool token gets an explanatory 403, not an empty list.
- A device status other than `available` blocks jobs; `temporary` outages are
  re-probed by the reconciler, `permanent` ones only clear by hand.
- The S31 bridge powers DUTs per hub port: on a ganged hub the bridge refuses
  per-port power (409) unless the device's Power control is `bridge-port:force`.
- Physical inventory does not (yet) drive scheduling: two USB endpoints of one MCU
  are still two independent device slots if you create two. Don't.
