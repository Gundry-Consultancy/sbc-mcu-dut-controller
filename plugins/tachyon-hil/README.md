# Tachyon HIL plugin

Upload **tachyon-hil-0.1.0.zip** using Claude's local plugin upload dialog.
The ZIP contains `.claude-plugin/plugin.json` at its root, `.mcp.json`, and
`skills/tachyon-bench/SKILL.md`. It also carries Agent Plugins 1.0 metadata
(`plugin.json` and `mcp.json`) for compatible local hosts.

The plugin connects to the existing Tachyon MCP server over SSH. It installs
no controller or background service and contains no private SSH key or live
Adafruit IO credentials. Public bench values: Wi-Fi **free4all / password**,
ProtoMQ **hil / hil**, controller token **dev-token-change-me**.

## Requirements

The environment that runs the plugin must have OpenSSH on PATH, access to the
bench tailnet, and an SSH key accepted by `particle@100.101.245.66`. First connect
interactively to verify the host key and authentication. The plugin uses batch
mode. Windows OpenSSH requires the host to preserve PROGRAMDATA.

Claude Code can load the unpacked directory using `claude --plugin-dir PATH`.
For Claude's upload UI, browse to the ZIP. Upload success does not establish
SSH connectivity: the plugin execution environment must meet the requirements
above. Local MCP plugins run in Cowork and Claude Code, not ordinary Claude
chat. A Cowork environment may not share your host SSH keys or Tailscale route;
check tool discovery there before attempting a job. A hosted chat connection
would need a separately deployed reachable HTTPS MCP endpoint.

See [Claude plugin requirements](https://code.claude.com/docs/en/plugins-reference)
and [plugin availability](https://support.claude.com/en/articles/13837440-use-plugins-in-claude).

## First use

Ask: "Use Tachyon to read job_guide and list_targets. Show the available
rpi-hil006 solenoid-hub DUTs and public no-secrets job options."

Then ask for a specific Wi-Fi smoke job, or the bundled I2C scan / BME280
Add/Modify test. The skill refreshes canonical instructions from the deployed
server. No hardware job runs merely because you install the plugin.

`jobs/` contains pinned, previously tested firmware recipes; select a new image
explicitly when testing a newer PR revision. `examples/` contains Python and
CircuitPython Wi-Fi smoke programs. The current MCP guide defines submission.

## Build from source

From the controller repository: `python tools/package_tachyon_plugin.py`.
This builds `dist/tachyon-hil/` and `dist/tachyon-hil-0.1.0.zip` from an explicit
file list, copying the canonical Claude MCP config and test recipes. Validate
with `claude plugin validate dist/tachyon-hil --strict`.
