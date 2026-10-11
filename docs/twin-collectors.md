# Collecting digital twin observations

`python -m hil_controller.twin_collectors` (or the installed-package wrapper
`scripts/collect-twin-observations.py`) emits JSON observations for review and
import. It does not open the controller database, bind USB devices to DUT slots,
open serial ports, change mux routes, reset hardware or switch power.

## Linux host capture

Run on the Linux host whose hardware is being inspected, for example through an
existing authorized SSH session with the controller package installed:

```sh
python -m hil_controller.twin_collectors \
  --host-id example-pi --scan-id inventory-2026-01-01-a \
  --output observations.json linux
```

The collector reads device-tree board model/serial, the SoC serial when exposed,
CPU architecture, USB descriptors, and tty symlinks whose sysfs ancestry belongs
to each observed USB endpoint. OS root hubs and interface directories are excluded.
No serial is an explicit `identity_status: unavailable`, never a generated hardware
serial. Other available identity sources, including firmware-mediated MCU silicon
UIDs, are outside this collector's scope.

Raspberry Pi's documented 64-bit board serial is recorded as verified board
identity. Other device-tree and SoC serials retain reported provenance and do not
claim verified uniqueness. A host slot is left unbound by default. The optional
`linux --bind-host-slot --expected-board-serial <expected-value>` binds only the
supplied host ID after that exact Raspberry Pi board serial is observed. Add
`--expected-twin-id <current-twin>` when explicitly replacing an existing slot
occupant; omitting it asserts the slot is empty. This never binds a DUT device slot.

## Explicit S31 / ESP USB/IP bridge capture

```sh
python -m hil_controller.twin_collectors \
  --host-id example-bridge --scan-id inventory-2026-01-01-b \
  --output bridge-observations.json bridge \
  --url https://bridge.example --token-env HIL_BRIDGE_TOKEN
```

Set `HIL_BRIDGE_TOKEN` privately in the execution environment if authentication is
required. Omit `--token-env` for a bridge that does not require authentication.
The collector makes one `GET /api/usb/devices` request to the supplied URL. It does
not discover arbitrary network hosts, follow redirects, enumerate power routes,
or invoke power APIs. Credentials in URLs are rejected; token values, response
error bodies and unrelated bridge fields are excluded from output. Transport and
schema errors fail collection instead of pretending that the inventory is empty.

## Endpoint records are not physical board claims

Every USB observation has `kind: usb_endpoint` and
`physical_device_association: unverified`. These are unresolved endpoint records,
**not countable MCU/DUT board twins**. Two USB ports can belong to one physical
MCU; a USB UART bridge or debug probe has its own identity, distinct from its target.
S31 records with `virtual: true` retain that descriptor flag and are likewise not
promoted to physical boards. Hubs without iSerial retain their descriptor and
location evidence but receive no unique hardware identity claim.

USB iSerial is recorded as a reported, non-unique `endpoint_serial` owned by the
named USB port. VID/PID, bus path, I2C address and model are locators or descriptors,
not per-unit identities. Associating endpoints with an existing board twin requires
a reviewed explicit board/port mapping and, where appropriate, an explicit
connection to a separate bridge or debugger twin. Neither matching port paths nor
matching iSerial strings automatically merge records or reassign logical slots.

## Review, replay and import

The observation ID is deterministic for the supplied scan ID, host ID, source and
locator. Save and replay the same captured JSON for an idempotent import. A fresh
hardware capture should have a new scan ID. `--observed-at <timezone-aware ISO
value>` can supply the capture timestamp; it otherwise defaults to the current
UTC time. Reusing a scan ID with changed data or timestamps is intentionally a
conflicting observation, not an overwrite.

Review results with the digital twin preview/import facility before writing them
to the database. Repeated new scans of endpoints without verified unique identities
need an explicit existing `twin_id` mapping, or they remain distinct unresolved
records. The collector does not silently reconcile those records. Scheduler
allocation deduplication across related endpoints/board twins is separate work;
these discovery records do not by themselves make it safe to allocate two ports
of the same MCU as independent DUTs.

For sensor identity support and the leased, explicitly routed I2C discovery
procedure, see [twin-discovery.md](twin-discovery.md). This collector performs no
I2C transactions.
