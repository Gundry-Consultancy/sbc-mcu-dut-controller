# PR1014 protobuf and real-hardware validation — 2026-10-14

The real QT Py ESP32-S3 N4R2 on the **rpi-hil006 solenoid USB hub** ran the
upstream PR1014 CI image against a fresh ProtoMQ build with pinned protobufs.
Jobs use public Wi-Fi **free4all / password** and broker credentials **hil / hil**.
No production or staging Adafruit IO credentials were used.

## Provenance

- [Arduino PR1014](https://github.com/adafruit/Adafruit_Wippersnapper_Arduino/pull/1014)
  head `93b4f5e33d0a9333b17f44a35b8117c4fb096c9a`, tested CI merge commit
  `5b3c89ff714592f8ba407210ab1ce638330e273f` with base
  `e1f41b91076dd0e6bbfe905faae452baebd50dcb`.
- [Firmware CI run 38579010387](https://github.com/adafruit/Adafruit_Wippersnapper_Arduino/actions/runs/38579010387),
  successful QT Py build job `115903601261`, artifact `11902967418`.
  Combined image: 3,150,160 bytes, SHA-256
  `38fbe1d439908beecf6457d8956bbd7b63893783db58f2fd6d1f696ac17218f7`.
- [Protobuf CI run 38257492494](https://github.com/adafruit/Wippersnapper_Protobuf/actions/runs/38257492494)
  head `be593b784cfa3ec15b434ea960c72d1ce84f8d08`, based on
  `b3688d612f3639a83d7415b3ef1a04572eb34ce6`. PR223 changes comments only.
- Actual ProtoMQ revision `3bf1afe80e308ff6f0bb07b4ece7c76cc6742dbc`.
  Every job ran `npm ci`, `npm run import-protos`, and `npm run build-web`.
  `BUILD_SOURCE` and `PROTOBUF_IMPORT_VERDICT` are in the job proof logs.

## I2C scan

Job `b74769e1-458f-40ae-944f-99215d7f5aad` finished with `result=pass`.
The controller selected `strand-hil006-air` through the live DB route:
analog mux `muxA` channel **2** to the QT Py, then the on-strand TCA9548 at
**0x77**, using string pins **D40 / D41**.

| Scan | Actual addresses returned by the firmware |
| --- | --- |
| Direct bus | 0x12, 0x51, 0x58, 0x77 |
| TCA channel 0 | 0x12, 0x18, 0x45, 0x46, 0x47, 0x4a, 0x51, 0x52, 0x58, 0x59, 0x61, 0x64, 0x74, 0x76, 0x77 |
| TCA channel 1 | 0x12, 0x45, 0x47, 0x48, 0x51, 0x53, 0x58, 0x59, 0x5c, 0x60, 0x77 |

All three scans had `status=ok` from actual Probed replies. A missing reply now
fails the stage; it cannot masquerade as an empty bus or a passing job.

## Compatibility and failures found

- The old HIL injector encoded pin fields as uint32. The updated schema uses
  strings. Attempt `9b5c139b-500d-434e-84f0-e64094e050fc` was rejected by the
  firmware with `wrong wire type`; the successful scan uses named pins. Integer
  pins remain supported explicitly for legacy firmware.
- String support was already merged upstream in Arduino
  [#943](https://github.com/adafruit/Adafruit_Wippersnapper_Arduino/pull/943),
  [#953](https://github.com/adafruit/Adafruit_Wippersnapper_Arduino/pull/953),
  [#959](https://github.com/adafruit/Adafruit_Wippersnapper_Arduino/pull/959), and
  Python [#281](https://github.com/adafruit/Adafruit_Wippersnapper_Python/pull/281).
  The controller fix reuses those firmware semantics.
- The initial Add/Modify pipeline waited for another check-in after the scan.
  Attempt `b3b272f2-c006-4c79-bd81-70d5e81d0ee8` was stopped; stages now reuse
  the device UID discovered within their job.
- BMP581 attempt `dfb0b95c-0d96-4443-b058-e53169d15b3e` accepted Add but logged
  repeated `Driver read failed (no valid sample)` and returned no sensor events.
  The assertion failed. The operator then confirmed that the BMP581 had been
  removed: the inventory label at 0x46 is stale, and the responding device may
  be another BMP5x. This is **not evidence of a BMP581 firmware regression**;
  its device identity was not verified. The BME280 on the same strand was
  selected for the settings proof.

Generic `inject_protobuf.payload_json` uses the current job's imported schema,
not handwritten field numbers. `T_PRESSURE` / `T_ALTITUDE` and numeric `6` / `27`
were proven to encode identically. Unknown enums, wrong map shapes, fractional
integer periods, unknown fields, and numeric pins in the current string schema
are rejected before publication. The imported bundle's SHA-256 is
`424b9f7859992caf69130d9b76cb732c2b1cba2f3624d955ddabc8d655cf5e01`.
Reproduce this check with `examples/mcp/check_protobuf_json.py --protomq-dir`
pointing at a saved job's ProtoMQ checkout.

## Client configs and reproduction

[Codex, Claude Code, and Gemini CLI configs](../examples/mcp/configs/README.md)
all connected to `particle@100.101.245.66` and discovered the 12 MCP tools.
The official MCP client launched the exact config commands. Codex config was
used to observe proof, Claude config to submit the scan, and Gemini config to
submit the Add/Modify pipeline. This verifies their stdio configuration and
protocol path; it is not a claim that three provider model sessions ran.

Windows OpenSSH needs `PROGRAMDATA` when a client filters its environment;
the example runner and Codex config now preserve it. No new persistent MCP
listener was installed. Tailscale reachability and an authorized SSH key are
still required.

Runnable jobs: [scan](../examples/mcp/pr1014-i2c.json),
[Add/Modify](../examples/mcp/pr1014-i2c-modify.json).
Machine-readable results, asset IDs, verdicts, and log hashes are in
[pr1014-results.json](../examples/mcp/pr1014-results.json).
Full serial, flash and broker logs remain retrievable through MCP job assets.


## Add, Modify, and settings effect: PASS

Job `25d402b2-3c4b-41ae-80a6-d5e2a0146d36` finished with `result=pass`, using
the same PR1014 image and pinned protobufs through the Gemini config.
The BME280 is on TCA channel 0 at **0x76**. Add used enum names;
Modify used numeric enums and a single `i2c.modify {remove, add}` message.

| Operation | sea_level_pressure setting | Valid events | Mean pressure (last 3) | Mean altitude (last 3) |
| --- | --- | --- | --- | --- |
| Add | 1000 hPa | 11 | 1021.713 hPa | -181.585 m |
| Modify: Remove + Add | 1100 hPa | 11 | 1021.728 hPa | +618.351 m |

Every checked sample matched `44330 * (1 - (pressure / reference)^0.1903)`
within 2 m. The approximately **800 m** derived-altitude change with stable
ambient pressure demonstrates that the updated custom setting took effect.
The test then removed the component. Cleanup released the lease, stopped the
per-job broker, and powered down the QT Py; it retains the PR1014 firmware
and public test credentials. This confirms this component/setting path,
not every enum, component driver, or custom setting.

Validation: **178 focused tests passed**, Ruff passed across src/tests/MCP
examples, and strict mypy with silent import following passed five new modules.
The latest upstream main and stacked PR base were fetched and remain
`a8d162c` and `1549ee8` respectively.
