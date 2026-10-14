# PR1014 latest-head hardware validation

**Latest-head HIL / ProtoMQ hardware retest: PASS (14 October 2026).**

Tested PR head `71209daaaef1963e11b5c44047576cd5f124cd7c` using CI merge `b407820a7049404b0fefd1900557f5df13b408dc` (base `0cb5eab5c693371a8b965edc73c2eff031c2e65a`). This supersedes the earlier hardware report for `93b4f5e3`.

Both jobs ran through MCP on Tachyon against the real **QT Py ESP32-S3 N4R2 on rpi-hil006's solenoid USB hub**. Each erased, flashed and verified the new image, wrote public bench credentials, connected to Wi-Fi and completed ProtoMQ check-in. Credentials: Wi-Fi `free4all` / `password`, broker `hil` / `hil`. No live Adafruit IO credentials were used.

**Provenance and reproduction**

- Firmware: [CI run 38584444797 / successful QT Py job 115922554572](https://github.com/adafruit/Adafruit_Wippersnapper_Arduino/actions/runs/38584444797/job/115922554572), artifact `11906702652`. The merge parents were verified against the PR head and base. Combined image: 3,150,160 bytes; SHA-256 `5dd28b75770dda58442d508c283cce4952b53975200845538abe49ccb85bf643`.
- Protobuf: [CI run 38257492494](https://github.com/adafruit/Wippersnapper_Protobuf/actions/runs/38257492494), pinned `be593b784cfa3ec15b434ea960c72d1ce84f8d08` (`api-v2` at `b3688d612f3639a83d7415b3ef1a04572eb34ce6` plus [PR #223's comment-only change](https://github.com/adafruit/Wippersnapper_Protobuf/pull/223)).
- ProtoMQ: `3bf1afe80e308ff6f0bb07b4ece7c76cc6742dbc`. Both jobs freshly ran `npm ci`, `npm run import-protos`, and `npm run build-web`; both recorded `PROTOBUF_IMPORT_VERDICT ok=true` and the expected source revisions.
- [HIL/MCP implementation and Codex, Claude, Gemini configs: PR #9](https://github.com/Gundry-Consultancy/sbc-mcu-dut-controller/pull/9). The scan used the Claude config; Add/Modify used the Gemini config via the official MCP client.
- [Machine-readable results and asset hashes](../examples/mcp/pr1014-latest-results.json), runnable [scan recipe](../examples/mcp/pr1014-i2c.json) and [Add/Modify recipe](../examples/mcp/pr1014-i2c-modify.json).

**I2C scans: PASS**

Standalone scan job: `7cf99b5a-89c8-492d-8cc3-963dcad3325a` (15:47:55–15:50:47 UTC). The Add/Modify job independently repeated all three scans. String pins `D40` / `D41`, analog strand mux `muxA` channel 2, TCA9548 at `0x77`.

| Scan | Firmware reply |
| --- | --- |
| Initial bus, standalone scan | `0x12 0x18 0x45 0x46 0x47 0x4a 0x51 0x52 0x58 0x59 0x61 0x64 0x74 0x76 0x77` |
| Initial bus, Add/Modify job | `0x12 0x51 0x58 0x77` |
| Explicit TCA channel 0, both jobs | `0x12 0x18 0x45 0x46 0x47 0x4a 0x51 0x52 0x58 0x59 0x61 0x64 0x74 0x76 0x77` |
| Explicit TCA channel 1, both jobs | `0x12 0x45 0x47 0x48 0x51 0x53 0x58 0x59 0x5c 0x60 0x77` |

**Test-harness clarification:** the initial scan does not reset a powered mux. Its first result is consistent with channel 0 retained from the previous job; it is not a guaranteed direct-only inventory. The controller log wording and canonical skill have been corrected to make this explicit. No firmware fix was required for the tested path.

**BME280 Add / Modify / custom-setting effect: PASS**

Job `82d941be-50dd-42d3-86bf-a04cd3d191cc` (15:51:07–15:54:27 UTC), BME280 at `0x76` on TCA channel 0:

1. Add with a 1-second period, enum names `T_PRESSURE` / `T_ALTITUDE`, and `sea_level_pressure.floatValue = 1000`.
2. A single `i2c.modify {remove, add}` with numeric enums `6` / `27` and updated `sea_level_pressure.floatValue = 1100`.
3. Check actual device-to-broker readings, then explicitly remove the component.

| Operation | Valid events | Mean pressure, last 3 | Mean altitude, last 3 |
| --- | --- | --- | --- |
| Add, 1000 hPa reference | 11 | 1021.019 hPa | -175.829 m |
| Modify, 1100 hPa reference | 11 | 1021.008 hPa | +624.213 m |

Each checked sample matched `44330 * (1 - (pressure / reference)^0.1903)` within 2 m. The **800.043 m** derived-altitude change at stable measured pressure confirms the updated setting took effect. These values are decoded MQTT measurements; the serial proof shows the corresponding firmware execution.

Both jobs finished with `result=pass`; cleanup stopped their brokers and powered down the DUT. Coverage is the online I2C path on this board and BME280 setting, including string pins and enum-name/numeric inputs. These jobs do **not** exercise the latest commits' offline SD-card JSON compatibility changes or production/staging endpoints.
