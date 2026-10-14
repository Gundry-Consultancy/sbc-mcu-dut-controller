# HIL MCP server

`hil-mcp` exposes leased HIL jobs over stdio or Streamable HTTP using the official
MCP Python SDK. Discover targets, read current skills, preview a recipe, submit,
poll its result, and retrieve proof logs/images. No model-provider key is needed.

## Install and connect

Use Python 3.12 and `pip install -e '.[mcp]'`. Set `HIL_API_BASE` to the reachable
controller and `HIL_API_TOKEN` to its bearer token. Run `hil-mcp` for stdio.
The controller must run this revision for CircuitPython and no-flash support.

Local OpenAI/Codex, Claude, and Gemini clients can launch the same executable.
Use its absolute path; alternatively launch the environment's Python with
`["-m", "hil_controller.mcp_server"]`. Pass credentials through private client
environment configuration. For clients using the common JSON configuration:

```json
{"mcpServers":{"hil":{
  "command":"/absolute/path/to/venv/bin/hil-mcp",
  "env":{"HIL_API_BASE":"http://127.0.0.1:8080","HIL_API_TOKEN":"REPLACE_PRIVATELY"}
}}}
```

Codex uses its MCP server configuration; Claude Desktop/Code and Gemini CLI use
`mcpServers`. The OpenAI Agents SDK can launch a stdio server; Gemini's Python
SDK can use an initialized MCP `ClientSession`.

Remote clients (including OpenAI Responses and Anthropic MCP connectors) use
Streamable HTTP. Set a separate `HIL_MCP_TOKEN`, then start:

```sh
hil-mcp --transport streamable-http --host 127.0.0.1 --port 8081
```

Connect to `/mcp` with `Authorization: Bearer <HIL_MCP_TOKEN>`. All HTTP methods
require authentication. Cloud clients need a reachable HTTPS reverse proxy;
put its hostname in `HIL_MCP_ALLOWED_HOSTS` (comma separated; include nonstandard
ports). Keep the loopback bind behind the proxy. DNS rebinding protection stays
enabled; unknown Host/Origin headers are rejected.

This is a **single controller principal** service. Every holder of its MCP token
uses the configured controller token's permissions; use separate instances for
different trust scopes. It is not an OAuth server. Clients requiring OAuth
discovery, including applicable ChatGPT connector flows, need an MCP-compatible
OAuth gateway. Public hosting and provider-side model calls are not included in
the protocol tests.

References: [OpenAI MCP](https://developers.openai.com/api/docs/guides/tools-connectors-mcp),
[Claude MCP](https://code.claude.com/docs/en/mcp),
[Gemini MCP sessions](https://ai.google.dev/gemini-api/docs/function-calling),
[official Python SDK](https://github.com/modelcontextprotocol/python-sdk).

## Credentials

**Public bench Wi-Fi: SSID `free4all`, password `password`.** Omit `secrets` for
test-only `IO_USERNAME=hil`, `IO_KEY=hil` and a per-job protoMQ broker. Its address
and ports come from the controller. Personal credentials are never read from
the MCP environment. Controller authentication and private-repo Git access
remain prerequisites; "no-secrets" refers to DUT Wi-Fi/IO credentials.

This guidance appears in MCP initialization, `job_guide`, the `secrets` schema,
`hil://job-guide`, authenticated `GET /v1/job-profiles`, and the raw job API's
OpenAPI description. Raw `POST /v1/jobs` still expects its existing job shape;
MCP builds these recipes on top of it.

| Broker | Endpoint | Required input | Controller profile |
| --- | --- | --- | --- |
| `protomq` | Allocated per job | No personal credentials | `bench-protomq` |
| `production` | `io.adafruit.com:8883` | `IO_USERNAME`, `IO_KEY` | `adafruit-production` |
| `staging` | `io.adafruit.us:8883` | `IO_USERNAME`, `IO_KEY` | `adafruit-staging` |
| `dev-lan` | Explicit host/port | `broker_host`, `broker_port`, IO credentials | `dev-lan` |

The controller token must allow the chosen profile, pool, and `trusted-firmware`
for MCU jobs. Profiles authorize access; they do not store credentials. Override
Wi-Fi with `secrets.WIFI_SSID` and `WIFI_PASSWORD`. Supply live IO values only in
`secrets`, never test code, params, or source URLs. Real IO values are rejected
in protoMQ mode. External modes never launch a local protoMQ broker.

- Arduino writes `secrets.json` through the existing `io_url`/`io_port` contract.
- Python gets IO/Wi-Fi credentials in its environment, and external routing as
  `MQTT_HOST/PORT` and `IO_URL/PORT`. The selected test must consume these fields.
- CircuitPython gets `settings.toml` containing `CIRCUITPY_WIFI_SSID/PASSWORD`,
  `ADAFRUIT_IO_USERNAME/KEY`, and `MQTT_HOST/PORT`.

Firmware/tests must support the chosen host and TLS where required. Local
`verify_checkin` and `inject_*` stages cannot establish cloud success and are
rejected for external recipes. Cloud MCU tests need a serial success marker
emitted after their actual cloud assertion, not merely a boot/Wi-Fi banner.

## Workflow and examples

1. Call `job_guide`, `list_targets`, and `list_skills`.
2. Read `hil-job-api` and the relevant current task skill with `read_skill`.
3. Call `prepare_job` to inspect a redacted request without running hardware.
4. Call `submit_job` with the **original AgentJob**, not the redacted preview.
5. Poll `wait_job` using `since=next_since` (each wait is at most 30 seconds).
6. Stop on finished/failed/cancelled/error/timeout; inspect result and verdicts
   with `list_assets` / `read_asset`. Finished or skipped tests do not prove success.

`cancel_job` invokes normal controller cleanup. Submissions are not automatically
retried: a connection failure may occur after acceptance. Check existing jobs
before retrying. `upload_firmware` and `read_asset` are bounded to 2 MiB; larger
artifacts use authenticated HTTP upload/download or firmware URL + SHA-256.

The following snippets are the `job` argument. Replace example IDs with discovered
device IDs and source refs/test paths with the actual desired revision/test.

WipperSnapper Arduino already installed, public protoMQ check-in:

```json
{"runtime":"wippersnapper-arduino","device_id":"DISCOVERED_MCU_ID"}
```

For an ESP32 combined binary add `firmware: {"path":"<uploaded path>",
"offset":"0x0"}`. The recipe seeds flash/verify → boot → secrets → reset →
check-in. UF2/SAMD/RP2040 targets need explicit `params.stages` from
`hil-author-test`; no chip family is guessed from a filename. Preserve that
board's flash ordering, then append secrets/reset/assertions. `requires` supplies
normal I2C strand requirements for the scheduler.

Python WipperSnapper, selected real-hardware test:

```json
{
  "runtime":"wippersnapper-python", "device_id":"DISCOVERED_SBC_ID",
  "pool":"wippersnapper-python",
  "source":{"repo":"https://github.com/adafruit/Adafruit_Wippersnapper_Python.git",
            "ref":"SELECT_TESTED_REF"},
  "params":{"entry":"/path/to/bench/venv/bin/python",
            "args":["-m","pytest","PATH_TO_SELECTED_HARDWARE_TEST","-v"]}
}
```

Choose interpreter, dependencies/setup, test path, and hardware flags from
`hil-display-pytest` and the source repo. The recipe launches protoMQ on the
controller; the test must honor `PROTOMQ_RUN_EXTERNALLY` and `MQTT_HOST/PORT`.
Private repositories need the bench host's Git access. No test filename is
hardcoded because the test repositories and skills are evolving.

CircuitPython already installed on a USB MSC target:

```json
{
  "runtime":"circuitpython", "device_id":"DISCOVERED_CIRCUITPY_ID",
  "files":{"code.py":"import os\nassert os.getenv('CIRCUITPY_WIFI_SSID')\nprint('HIL_CPY_EXAMPLE_OK')\n"},
  "expect_serial":"HIL_CPY_EXAMPLE_OK"
}
```

This example proves deployment/settings/reset/serial execution, not networking.
Supply code that performs the desired MQTT/sensor assertion before printing its
marker. UTF-8 `lib/*.py` files are supported; binary `.mpy` dependencies must be
preinstalled. Only supplied files and settings are replaced, with `code.py` last.
The stage syncs and unmounts even on failure. `boot.py` is not edited; the runtime
must expose a host-writable CIRCUITPY volume. Auto-reload can occur during writes;
the recipe checkpoints serial before its final reset and asserts subsequent
output. Settings persist on the DUT like Arduino secrets; run a public recipe
after live testing to replace credentials. Use a unique marker per test run.

## Updating skills

Editable installs read `.agent/skills/*/SKILL.md` on every call. Wheels include
a snapshot. Set `HIL_SKILLS_DIR` to a maintained canonical directory to refresh
skills without restart/reinstall. `list_skills` returns content hashes and finds
new skills automatically. Changes to executable stages still require updating
the controller/MCP installation. No automatic pull/merge or schedule is created.

Automated tests use real MCP stdio/HTTP sessions with fake controller/hardware
transports. For the real bench Wi-Fi/check-in results and tested revisions, see
[mcp-bench-validation-2026-10-14.md](mcp-bench-validation-2026-10-14.md).
The repeatable MCP client and smoke programs are in `examples/mcp/`.

### Running directly on the bench

No Git deployment or persistent MCP listener is required. Install the MCP extra
alongside the controller and run `python -m hil_controller.mcp_server` over stdio.
An SSH session can run both client and server on Tachyon. From the controller's
checkout, with `HIL_API_BASE` and `HIL_API_TOKEN` exported:

```sh
.venv/bin/python examples/mcp/run_job.py --job /tmp/job.json --output /tmp/proof \
  --command .venv/bin/python -- -m hil_controller.mcp_server
```

`run_job.py` uses MCP for discovery, preparation, submission, waiting, and proof
retrieval. It saves the submitted ID immediately; use `--job-id ID` to resume
observation without submitting twice. The SDK filters inherited variables, so
the example explicitly forwards controller configuration. Keep real tokens in
the environment, not command arguments. A finished state must also have a pass
result; inspect the saved verdicts to establish what was actually tested.

The controller's `HIL_CONTROLLER_IP` must be its current DUT-reachable LAN
address. Its API can use a Tailscale address while its Wi-Fi DUTs need the LAN
broker address. Restart the controller after changing this setting when idle.
CircuitPython storage may enumerate later than serial; `write_circuitpython`
waits up to 30 seconds (`msc_timeout_s`, maximum 120). Partitioned MSC volumes
use their first numbered partition. A read-only volume needs a suitable
`boot.py` or a clean board-matched firmware installation before deployment.
