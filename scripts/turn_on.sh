#!/usr/bin/env bash
#
# turn_on.sh <channel 0-7> [on_duration_seconds]
#
# Send the ON pulse to a solenoid-hub USB port (powers the port up).
#
# The optional second argument sets the ON-pulse *hold* in seconds — i.e. how
# long the button is "pressed". Defaults to 0.2s, matching the hub logic that
# distinguishes a short ON pulse from the longer OFF pulse.
#
# Thin wrapper over the vendored solenoid_hub_cli.py (single source of truth for
# MCP23017 timing). Resolves the CLI relative to this script's real location
# (follows symlinks), falling back to the standard /opt/hil/ deploy path.

set -euo pipefail

usage() {
    echo "usage: $(basename "$0") <channel 0-7> [on_duration_seconds]" >&2
    exit 2
}

ch="${1:-}"
[ -n "$ch" ] || usage
dur="${2:-0.2}"

here="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cli="$here/solenoid_hub_cli.py"
[ -f "$cli" ] || cli="/opt/hil/solenoid_hub_cli.py"

exec python3 "$cli" port_on "$ch" --on-duration "$dur"
