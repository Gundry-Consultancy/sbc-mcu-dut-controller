# WipperSnapper v2 through the real IO stack (staging)

A bench suite that checks the Adafruit IO web UI's WipperSnapper v2 paths
against a real DUT on a **staging** (or dev-lan) IO deployment, using the
`io_api_component` / `io_ws_publish` / `io_ws_expect` stages (see
[`docs/api.md`](../../docs/api.md#real-io-stack-stages-broker-staging--dev-lan)).
protoMQ jobs prove the firmware handles the protobuf. These jobs prove the rest
of the path: REST API → `ws-r2b`, web → `ws-w2b`, and the bridge's
`ws-b2w` replies, timed the way the UI times them.

`ws_v2_suite.py` renders the jobs, runs them through
[`examples/mcp/run_job.py`](../mcp/run_job.py), and judges the logs. Every
verdict-producing stage carries a `label`, which the stages echo in
`IO_API_VERDICT` / `IO_WS_PUBLISH` / `IO_WS_VERDICT` / `IO_INFRA_ERROR`.

## Probes

| job | probe | question | bug when |
|---|---|---|---|
| `v2-real-io` | `checkin` | Does a checkin arrive? Captures the device key. Listens on both `ws-b2w` variants. | — (*inconclusive* if the first match was `ws-b2w/<key>`; `ui-topic` decides) |
| | `probe-1`…`probe-N` | Does an `i2cProbe` get `i2cScanResults` inside the UI's 5 s staging timer? `probe-1` is the first after checkin and listens **only** on bare `{user}/ws-b2w`, as the UI does. | `elapsed_s` > 5 (measured against a 15 s budget) |
| | `ui-topic` | Do replies reach the bare `ws-b2w` the UI subscribes to? (from `probe-1` vs the wider probes) | replies arrive on `ws-b2w/<key>` but never on bare `ws-b2w` |
| | `scan-all` | Does `i2cScanAll` get a reply? | — (*inconclusive* if not; the v2 reply shape is unconfirmed) |
| | `add` | Does a REST create emit `addComponent` on `ws-r2b`? | 5xx, or no frame |
| | `modify-period` | Does a pin-info change emit `modifyComponent` with the new period? | 5xx, or no frame |
| | `rename-only` | Does a name-only update succeed and emit `modifyComponent`? | 5xx, or 2xx with no frame (*fixed* when both work) |
| | `rename-same-pin-info` | Same, sending the unchanged `wipper_pin_info` | as above; a success is only *inconclusive*, because the stored pin info may carry fields the request lacks. `rename-only` is the authoritative probe |
| | `remove` | Does a REST delete emit `removeComponent`? | 5xx, or no frame |
| `v1-ui-assisted-staging` | `v1-registered` | Does a 1.x DUT register on this deployment? | (*prereq* if not) |
| | `v1-ui-add` | Does a component **added in the IO web UI by a person** reach a 1.x device? | the add marker never appears in 600 s |
| `v1-ui-assisted-production` | same | Baseline for comparison (only with `--v1-production-baseline`) | |

A run starts with a soft delete of the test feed (`cleanup-before`), and every
lifecycle stage is soft, so a run that stopped early can't block the next one.
The feed key is fixed (`i2c-{sda}-{scl}-0x{addr}-{sensor}`, the UI's own shape).

### Statuses

- **pass**: behaved as the UI expects.
- **bug**: reproduces a defect.
- **fixed**: a previously failing check now behaves correctly.
- **fail**: wrong, but not one of the named defects.
- **infra**: auth, ACL, throttle or network (`IO_INFRA_ERROR`). Not a verdict; fix the bench and rerun.
- **prereq**: a precondition is missing (see below). Not a verdict.
- **not_run**: an earlier stage stopped the job.
- **inconclusive**: the result can't settle the question (an unconfirmed reply shape, or a probe that can't tell apart the cases).

## What this can't prove

The IO web UI publishes some messages itself, from the browser: v1 component
add/remove/batch, device delete, and its error and scan-result handling. A
REST call doesn't exercise those paths. The assisted v1 job covers the v1
component path: it holds the DUT while a person adds a component in the UI,
then asserts the device saw it. Other browser-side behaviour needs a browser
test, not a bench job.

## Prerequisites

1. **An AIO key** for the IO account, exported as `IO_KEY` when you run. It's
   never rendered into a job file. `run_job.py --secret-env IO_KEY` adds it in
   memory, and the MCP server redacts it from every saved response. Use a key,
   never a password.
2. **The DUT registered on that account once.** A device becomes an IO group
   when someone accepts it in the UI, or with
   `POST /api/v2/{user}/groups {"group": {"key": <checkin deviceId>,
   "machine_name": <args.machineName>, "wipper_semver": <args.semver>}}`
   (the checkin `deviceId` is the group key). Until then the lifecycle probes
   report *prereq* (HTTP 404). IO stores a device's feeds under
   `{device_key}.{key}`, so updates and deletes reuse the key the create returns.
   Once registered, the bridge sends **no** `ws-b2w` checkin for the device
   (only unregistered devices get one), so pass `--device-key <group key>`:
   every `io_*` stage gets the key and a missing checkin reports *info*.
3. **2.x firmware that speaks the deployment's v2 protocol** (`--firmware-json`,
   an AgentJob firmware object). If no checkin arrives, the result is *prereq*.
4. **The exact `wipper_pin_info`.** The default is a BME280 sketch. For a real
   run, copy `wipper_pin_info` from a feed the UI created for the same sensor
   (`GET /api/v2/{user}/groups/{device_key}/feeds`) into `--pin-info-json`.
5. **For the assisted v1 job:** a 1.x build (`--v1-firmware-json`) and the
   serial line it prints when a component is added (`--v1-add-marker`, taken
   from a known-good log).
6. A shared account with several DUTs: pass `--machine-name` so the checkin
   probe captures the right device.

## Run

```bash
python examples/io-staging-ws-v2/ws_v2_suite.py render --io-user "$IO_USERNAME" \
  --device-id mcu-qtpy-esp32s3-n4r2-hil006 --firmware-json fw-ws2.json \
  --sda 41 --scl 40 --address 0x76 --out run/io-suite/jobs
```

```bash
IO_KEY=... python examples/io-staging-ws-v2/ws_v2_suite.py run \
  --jobs run/io-suite/jobs --out run/io-suite/results \
  --mcp-config examples/mcp/configs/claude.mcp.json
```

```bash
python examples/io-staging-ws-v2/ws_v2_suite.py evaluate --results run/io-suite/results
```

`render` validates every job through the same job-profile builder MCP's
`prepare_job` uses, so a gating or shape error shows up before any hardware is
touched. `evaluate` writes `verdicts.json` and `verdicts.md`. The offline tests
(`tests/test_io_staging_suite.py`) run these stages against an in-memory broker
and a mock REST API: once modelling the defects, once modelling them fixed.
