# Real MCP bench validation — 2026-10-14

All three runtimes passed real jobs submitted, polled, and read through the
MCP server. The official Python MCP client ran on Tachyon over stdio, launched
from SSH at `particle@100.101.245.66`. No new network listener was installed.
Public Wi-Fi was **free4all / password** and protoMQ credentials were **hil / hil**;
no personal Adafruit IO credentials or production/staging endpoints were used.

Only the available `rpi-hil006` solenoid-hub bench was used. `rpi-displays` and
its unavailable DUTs were excluded.

| Runtime | Real target | Job ID | Proof |
|---|---|---|---|
| WipperSnapper Python | Raspberry Pi 4B, `rpi-hil006-pi4-a` | `09ea9fd6-d556-4005-aba7-d3d543be98ac` | `MCP_PY_WIFI_OK 192.168.1.188 free4all`; two Wi-Fi pings received; `MCP_PY_CHECKIN_OK` |
| WipperSnapper Arduino | QT Py ESP32-S3 N4R2, `mcu-qtpy-esp32s3-n4r2-hil006` | `4eebd473-5f0d-41c8-87ab-7725f1d39c3f` | `Connected to WiFi!`; `CHECKIN_VERDICT ok=true ... proto=v1` |
| CircuitPython | Same QT Py | `8d703665-7ca1-4a72-88f0-101c918205e5` | Post-reset `MCP_CP_WIFI_OK 192.168.1.203 free4all`; `SERIAL_ASSERT_VERDICT ok=true` |

Each job ended `finished` with `result=pass`. The Python ping explicitly bound
to `wlan0`; its separate MQTT check-in used the normal routing table, which can
use Ethernet. CircuitPython tested Wi-Fi only, not MQTT. Arduino check-in proved
Wi-Fi plus the local broker handshake. These are smoke tests, not full runtime
or cloud compatibility certification.

Machine-readable job records, asset IDs, hashes of MCP-returned log text, and
short proof excerpts are in `examples/mcp/bench-results-2026-10-14.json`. Full
logs remain controller job assets and can be retrieved with MCP `list_assets`
and `read_asset`. `run_job.py --job-id ID` resumes proof collection without
resubmitting a job.

## Tested software

- MCP SDK 1.30.0; PR #9 code deployed directly into Tachyon's existing controller
  checkout, preserving its digital-twins branch and topology. Executable
  changes were loaded by restarting the existing service while idle.
- WipperSnapper Python 2.0.0-alpha.1, clean installed checkout
  `00d56aabf094890e53f76a4fc22c8731ed9a2aaa`, Python 3.13.5 on the Pi. The job
  cloned that local repository and used its existing virtual environment.
- WipperSnapper Arduino 1.0.0-beta.130, staged board-matched combined image,
  SHA-256 `5bf38027f6d2a96d7a8ce906064bddf5d2ff2565c907af97b9c1ba351061f49b`.
- CircuitPython 10.3.1, official QT Py ESP32-S3 4MB flash / 2MB PSRAM image,
  SHA-256 `37c8ab85db6d64bb13a4a017f8c19728dc0684e66131b0b9f735fd203a41fb70`.
  Source: https://circuitpython.org/board/adafruit_qtpy_esp32s3_4mbflash_2mbpsram/

## Failures found and resolved

1. Tachyon's configured broker address was the stale `192.168.1.169`. Its current
   Ethernet address was `192.168.1.145`; `HIL_CONTROLLER_IP` was corrected on the
   bench. The API remained reachable at its Tailscale address.
2. The first Arduino no-flash attempt (`42b0a1fa-be90-466c-a732-3fccd2d43437`)
   found CircuitPython already installed. Its partitioned disk exposed a stale
   WIPPER label, so the label alone did not identify the running firmware. The
   MSC resolver now chooses the numbered partition instead of the whole disk.
3. CircuitPython attempt `5c74fb5a-a24a-4b3f-941e-08bdc85e757d` reached serial
   before storage enumerated. Deployment now waits for MSC with a bounded
   timeout; no-flash recipes explicitly power-cycle before writing.
4. Attempt `8345d7c5-07b1-4006-8650-443e4cedcc6e` reached storage but the existing
   CircuitPython setup was host-read-only. Installing a clean official image
   through the MCP job resolved it. Host-writable storage remains a documented
   prerequisite; boot.py is not silently rewritten.
5. Native USB loader entry required the controller's existing cold-power-cycle
   and USB-JTAG recovery. Subsequent recipes shortened the initial touch retries.
6. CircuitPython's REPL output exposed terminal echo feedback in serial capture.
   The capture command now explicitly disables echo, including on reconnect.
   Final MCP job `8d703665-7ca1-4a72-88f0-101c918205e5` passed with clean serial
   proof after this correction. The earlier passing CircuitPython job was
   `5ff5a6f1-c7e7-4c4a-97dd-74d33f924fd0`.

The Pi's temporary Wi-Fi profile was deleted and wlan0 returned to disconnected;
Ethernet management remained connected. Controller jobs release their leases,
stop their per-job brokers, and power down the MCU during cleanup.

The final MCU image is CircuitPython 10.3.1 with the public Wi-Fi smoke program.

## Regression validation

The affected MCP, CircuitPython, MSC, serial-capture, and firmware-bench tests
passed: **105 tests**, one existing pytest-asyncio deprecation warning. Ruff
passes across `src`, `tests`, and the new examples. Strict mypy with silent
import following passes the four new MCP/profile/CircuitPython modules.
Upstream main (`a8d162c`) and the stacked PR base (`1549ee8`) were fetched again
before publishing; both remained current.
