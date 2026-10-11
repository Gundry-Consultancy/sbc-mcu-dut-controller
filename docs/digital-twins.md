# Physical digital twins

The `/v1/twins` administrative API keeps physical specimens separate from existing
runtime topology rows. A `devices.id` remains a logical schedulable slot for backward
compatibility. A physical twin gets an opaque persistent UUID. Replacing a board or
sensor creates another UUID and closes the previous slot assignment; the old unit,
its identifiers, observations, and wiring history remain queryable.

This inventory is additive. Startup creates the new tables on both fresh and existing
databases; it does not invent units from old USB serials or change topology YAML,
job selection, leases, flashing, power control, or the S31 bridge transport.
Existing topology reseeding does not overwrite twins. A logical slot must exist before
it can be assigned. Slot references are validated against a fixed table allowlist
inside the write transaction; they are polymorphic references rather than SQLite
foreign keys. Deleting a legacy slot does not erase its physical assignment history.

## Identity and evidence

Every observation has a caller-issued `observation_id`, source, timezone-aware
`observed_at`, server receipt time and authenticated actor. Its full payload is retained.
Replaying the same ID and normalized content is idempotent; reusing it for different
content returns HTTP 409. Mutations use a SQLite immediate transaction: any identity,
slot or ownership conflict rolls back the complete observation, including new ports.

An identifier has `namespace`, `kind`, `value`, `owner`, `component`, `unique`,
`confidence`, and `evidence`. Use a manufacturer/product identity scope for namespace;
values remain case-sensitive and are never guessed or normalized by the controller.
Collectors must normalize documented numeric encodings consistently. Supported kinds:

| Kind | Ownership | Example namespace |
|---|---|---|
| `silicon_uid` | A named chip, such as `soc` | `test.vendor:soc-family:uid` |
| `board_serial` | Board | `test.vendor:board:serial` |
| `manufacturer_serial` | Board or named chip | `test.vendor:sensor-family:serial` |
| `endpoint_serial` | An owned port ID | `usb:test-product:iserial` |
| `inventory_tag` | Board or named chip | `operator:test-lab:asset` |

A USB-to-UART bridge serial belongs to that physical bridge and its USB endpoint,
not to an attached MCU. A debugger is its own twin with its own USB identity and
SWD/JTAG/UART output ports. USB iSerial strings are endpoint identifiers unless a
separate documented hardware read establishes a silicon/board identity. Duplicate,
placeholder or firmware-generated iSerial values are not unique claims.

`unique: true` requires `confidence: verified`. Verification is an assertion by the
trusted collector/operator with evidence, not cryptographic attestation by this API.
A unique namespace/kind/value cannot be claimed by another specimen or a different
component. A different unique value for an existing specimen's same owned identity
returns 409 and requires an explicit replacement. There is no automatic merge, identity
transfer, or deletion endpoint. All raw observations remain available when confidence
is promoted; the identity's evidence pointer updates to the promoting observation.

Model numbers, chip identification registers, I2C addresses, USB VID/PID, hostnames,
MAC addresses and bus paths belong in attributes/ports, never in identifier kinds.
Sensors without readable serials still get a UUID and optional operator inventory tag.
Record `identity_status: "not_exposed"`, `"unknown"`, or `"read_blocked"` as appropriate.
A configured but unverified specimen may be represented with
`verification: "configured_not_verified"`; this is not evidence that it was observed
or that a manufacturer's serial was read. Never fabricate a serial from its address.

## Discover, review, ingest

All endpoints require bearer auth and global pool access (`allowed_pools: ["*"]`).
Reads require `inventory:read` or `inventory:write`; writes require `inventory:write`.
The bootstrap static token has these permissions. Physical hosts and shared peripherals
span scheduler pools, so partial-pool tokens cannot traverse this administrative graph.

1. `POST /v1/twins/observations/preview` with an observation. This validates the entire
   proposed write in a rolled-back transaction and returns exact verified unique identity
   candidates. It never matches address/model/VIDPID or silently picks among candidates.
2. Review the evidence. If an existing physical unit is identified, set its `twin_id`
   explicitly and submit a new observation. A new scan ID is not a physical identity.
   Units without verified unique identifiers require an operator-maintained mapping;
   repeated scans with new IDs and no `twin_id` otherwise create separate specimens.
3. `POST /v1/twins/observations` applies the reviewed observation. Missing `twin_id`
   creates a unit, requiring `kind` and `label`. A known unique identity prevents creating
   a duplicate and asks the caller to choose its explicit twin ID.

Example, entirely synthetic:

```json
{
  "observation_id": "fixture-scan-1",
  "source": "operator:fixture",
  "observed_at": "2026-01-01T00:00:00Z",
  "kind": "microcontroller",
  "label": "Example MCU",
  "pool": "public",
  "identities": [{
    "namespace": "test.vendor:soc:uid",
    "kind": "silicon_uid",
    "value": "SYNTHETIC-001",
    "owner": "chip",
    "component": "soc",
    "unique": true,
    "confidence": "verified",
    "evidence": "Synthetic example; real collectors describe the register/probe used"
  }],
  "attributes": {
    "manufacturer": "Example",
    "model": "Example board",
    "architecture": "arm",
    "psram_bytes": 0,
    "build_profiles": [
      {"system": "arduino", "fqbn": "example:arm:board"},
      {"system": "circuitpython", "board_id": "example_board"}
    ]
  },
  "ports": [
    {"id": "usb-native", "kind": "usb", "mode": "runtime"},
    {"id": "usb-uart", "kind": "usb"},
    {"id": "debug", "kind": "swd"},
    {"id": "power", "kind": "power"}
  ]
}
```

Attributes are extensible evidence, not validated scheduler/build capabilities.
The current attribute view shallow-merges observation keys by `(observed_at,
observation_id)`; a nested object/list replaces its previous value, and JSON null is
an explicit value. Missing keys remain unchanged. Zero PSRAM is distinct from unknown.
All old attributes and firmware/build identities remain in observation history.
Port snapshots use the same ordering, and a port kind cannot silently change.
Use distinct named ports/endpoints for different physical connectors or firmware modes
when their identities must coexist. A single MCU can have native USB runtime/bootloader,
an external bridge connection, UART, SWD and JTAG without becoming multiple DUTs.

## Slots and replacement

`GET /v1/twins/slots?kind=device` lists logical slots, current occupants, and history.
Other kinds are `host`, `strand_component`, `peripheral`, `aux`, and `camera`.
An observation may explicitly attach its twin using:

```json
{"slot": {"kind": "device", "id": "existing-logical-slot", "expected_twin_id": null}}
```

Null is mandatory when asserting an empty slot. Replacements supply the actual previous
UUID as `expected_twin_id` and a later observation time. A stale occupant expectation
returns 409. A USB port previously occupied by a PyPortal that now enumerates a Debug
Probe must stay unresolved until reviewed; a port address alone never assigns either
physical identity. Reassigning a slot does not automatically rewire its physical cables.
Close/open the relevant connections explicitly using their physical twin IDs.

One twin may occupy multiple logical roles (for example a Linux host also used as a DUT).
**The current scheduler still leases logical devices, not physical twins.** This API does
not yet deduplicate two schedulable slots assigned to the same specimen. Operators must
avoid configuring both as simultaneously schedulable aliases. Multi-device role planning,
whole-route leases, capability matching and automatic mux control remain future work.

## Connections and topology history

`POST /v1/twins/connections` accepts an immutable caller-generated ID, `from_twin_id`,
`from_port`, `to_twin_id`, `to_port`, `kind`, `source`, `confidence`, `evidence`, and
timezone-aware `started_at`. Both ports must exist on their stated physical owners.
`state` is `connected` or `potential`; optional attributes can describe mux controller,
channel, I2C address, routing condition, voltage, supplies_power, switching domain,
or capture purpose. Potential paths describe available wiring, not an active reservation.
Kinds are extensible (USB, I2C, SWD, JTAG, UART, power, capture, containment).

`POST /v1/twins/connections/{id}/close` takes `ended_at`, `source`, and `reason`.
It retains the old endpoints and provenance. Exact repeats are idempotent; different
content for an existing connection/closure returns 409. Use a new connection ID after
rewiring. Record every possible power source, including probe power and secondary USB;
this graph alone does not establish that a proposed cold reset removes all power.

`GET /v1/twins` lists units with optional `kind`, `pool`, `limit`, and `offset` filters.
`GET /v1/twins/{uuid}` includes attributes, identities, ports, observations, connections,
and slot history. Observations and history currently have no pagination; keep this in
mind for very high-frequency collectors. These endpoints are inventory operations, not
telemetry streaming. Exact serials should remain in the private runtime DB/evidence,
not committed test fixtures or public logs.

## Offline import and migration

Use `python -m hil_controller.twin_import --db controller.db --input observations.json`
(or installed command `hil-twins-import`). The default makes a consistent SQLite backup
into a temporary copy, applies/validates there, and discards it. The original schema and
data stay unchanged. Returned dry-run UUIDs are provisional.

To persist, stop competing maintenance writers, then pass
`--apply --backup path/to/new-backup.db`. The command requires an existing DB and a new
backup path, takes a SQLite online backup (including committed WAL state), and atomically
installs only the additive twin tables plus the complete batch. It does not run unrelated
controller migrations. A conflict rolls back the full batch and its schema creation.
The backup remains available after a failed apply. Review the input and deployment
compatibility before operating on a production DB.

The input can be an observation list or an object with `observations` and `connections`.
Within batch connections, `from_twin_id` / `to_twin_id` may use
`"observation:<observation_id>"` to reference a unit resolved earlier in the same batch.
This permits importing one physical graph without guessing newly generated UUIDs.
Replaying the same batch preserves IDs and does not duplicate graph records.
