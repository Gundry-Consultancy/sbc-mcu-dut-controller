# Connect an agent to Tachyon

These configs launch the deployed MCP server over SSH at `particle@100.101.245.66`
(Tailscale). The server talks to the controller at `127.0.0.1:8080` using the
public development token `dev-token-change-me`. No local HIL installation or
model-provider API key is needed to establish this MCP connection.

Prerequisites: access to the bench tailnet, OpenSSH on PATH, and an SSH key
accepted by `particle`. First run `ssh particle@100.101.245.66` interactively to
verify the host key and authentication. Configs use batch mode and cannot answer
password or host-key prompts. They execute the current deployed checkout, so
skill updates appear on the next read without copying skills into every client.

| Client | Config in this directory | Installation |
| --- | --- | --- |
| Codex CLI / app | `codex.toml` | Merge its table into `~/.codex/config.toml`; restart/reconnect MCP. |
| Claude Code | `claude.mcp.json` | Merge into your project's `.mcp.json`, or run `claude --mcp-config examples/mcp/configs/claude.mcp.json`. Approve the project server when prompted. |
| Gemini CLI | `gemini.settings.json` | Merge `mcpServers.tachyon` into `~/.gemini/settings.json` or the project's `.gemini/settings.json`; restart and inspect `/mcp`. |

Preserve your existing settings when merging. All three use the same stdio
launch command. On Windows, prefer Windows OpenSSH if another `ssh` shadows it
(set `command` to `C:/Windows/System32/OpenSSH/ssh.exe`). Preserve `PROGRAMDATA`
in any custom environment filter: Windows OpenSSH exits silently without it.
Codex's `env_vars` and the example Python runner preserve that variable.

Ask the agent: "Use Tachyon to read job_guide, list_targets, and the current
hil-job-api skill. Run the no-secrets job on the rpi-hil006 solenoid-hub bench,
then report its verdict and evidence." MCU DUTs on `rpi-displays` are unavailable.
Always discover current targets and strand routes before allocating hardware.

Public Wi-Fi is **free4all / password**; ProtoMQ test credentials are **hil / hil**.
Omit `secrets` for these tests. Production (`io.adafruit.com`), staging
(`io.adafruit.us`), and explicit dev-LAN brokers accept optional live credentials
through the job's `secrets` field; see [the MCP guide](../../../docs/mcp.md).

For a reproducible protocol client (install `.[mcp]` locally):

```sh
python examples/mcp/run_job.py --mcp-config examples/mcp/configs/codex.toml --job examples/mcp/pr1014-i2c.json --output proof/pr1014
```

That example flashes PR #1014's QT Py firmware already staged on Tachyon, pins
the matching protobuf revision, and probes the shared I2C strand. It requires an
available QT Py ESP32-S3 N4R2 on rpi-hil006. Firmware SHA-256 is checked by the
controller. These SSH configs serve local CLI/app agents; hosted cloud connectors
need a separately deployed reachable HTTPS MCP endpoint.

Official format references: [Codex MCP](https://developers.openai.com/codex/mcp/),
[Claude Code MCP](https://code.claude.com/docs/en/mcp),
[Gemini CLI MCP](https://geminicli.com/docs/tools/mcp-server/).

To verify Add/Modify and custom settings, use
`examples/mcp/pr1014-i2c-modify.json` with the same runner. It requires the BME280
strand capability and verifies measured pressure/altitude against the configured
reference after both Add and Modify. See the
[hardware report](../../../docs/mcp-pr1014-validation-2026-10-14.md).
