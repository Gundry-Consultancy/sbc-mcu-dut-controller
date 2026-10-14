# MCP Wi-Fi smoke jobs

Install the controller's `[mcp]` extra. `run_job.py` speaks the MCP protocol to
its subprocess; all job actions and proof reads go through MCP. It accepts an
AgentJob JSON file or `--job-id` to resume observation. See `docs/mcp.md` for
client invocation and authentication.

The public defaults are Wi-Fi **free4all / password**, with protoMQ **hil / hil**.
No personal Adafruit IO credentials are used by these smoke tests.

- **Arduino:** select a real MCU from `list_targets`, provide a matching
  WipperSnapper firmware artifact if needed, and use the default recipe. A
  successful `verify_checkin` proves Wi-Fi and the local broker handshake.
- **CircuitPython:** place the contents of `circuitpython_wifi.py` in
  `files["code.py"]`, set runtime `circuitpython`, select the MCU, and set
  `expect_serial` to `MCP_CP_WIFI_OK`. Supply board-matched firmware if necessary.
  The code reports the acquired IPv4 address and associated SSID repeatedly so
  the serial capture can reconnect after a cold reset. It tests Wi-Fi only.
- **Python:** select an SBC and its actual WipperSnapper interpreter. Set runtime
  `wippersnapper-python`, `source.repo/ref` to the intended checkout, and
  `params.args` to `["-u", "-c", <contents of wippersnapper_python_wifi.py>]`.
  The tested Pi used pool `wippersnapper-python`. The script needs NetworkManager,
  passwordless sudo for `nmcli`, Ethernet management, and a disconnected wlan0.
  It creates a temporary non-default-route Wi-Fi profile, pings the broker over
  wlan0, completes a real WipperSnapper handshake, and deletes the profile.
  The MQTT connection uses the normal routing table and may use Ethernet.

The Python example imports the interpreter's installed WipperSnapper package;
record that installed revision as well as the job's source revision. The source
can be an existing local repository on the target, avoiding a network clone.
The smoke examples deliberately do not install firmware or dependencies by
themselves; deployment remains owned by the submitted HIL job.

Results and firmware hashes from the real run are recorded in
`docs/mcp-bench-validation-2026-10-14.md`.

Ready-to-use [Codex, Claude Code, and Gemini CLI configs](configs/README.md)
connect over SSH to Tachyon. The [PR1014 I2C job](pr1014-i2c.json) also pins
updated protobuf sources and checks real scan replies.
