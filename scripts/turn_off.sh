#!/usr/bin/env bash
#
# turn_off.sh <channel 0-7> [off_hold_seconds]
#
# Power a solenoid-hub USB port OFF with a *generous, state-resetting* sequence,
# because the soft-latch buttons toggle and their exact timing varies — a single
# blind OFF press can land in the wrong state or not fully depower a board whose
# USB has wedged. The sequence (all via the vendored solenoid_hub_cli.py, which
# owns the MCP23017 timing):
#
#   1. short press      → turn ON (normalise the latch to a known powered state)
#   2. wait             → let it settle
#   3. press and hold   → the OFF press (off_hold_seconds, default 3.0)
#   4. release + wait   → let the port fully depower (capacitors discharge)
#
# The optional 2nd arg is the OFF-press hold in seconds; the depower wait scales
# with it. A long hold (e.g. `turn_off.sh 4 10`) is the most reliable way to
# clear a wedged native-USB board.
#
# Resolve the CLI relative to this script's real location (follows symlinks,
# e.g. ~/turn_off.sh -> repo scripts/), falling back to the /opt/hil/ deploy path.

set -euo pipefail

usage() {
    echo "usage: $(basename "$0") <channel 0-7> [off_hold_seconds]" >&2
    exit 2
}

ch="${1:-}"
[ -n "$ch" ] || usage
hold="${2:-3.0}"

here="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cli="$here/solenoid_hub_cli.py"
[ -f "$cli" ] || cli="/opt/hil/solenoid_hub_cli.py"

# on_first (short ON, ~0.3s) resets the latch to ON; sleep-between (2.0s) waits;
# off-duration is the held OFF press; post-off-s waits for full depower.
exec python3 "$cli" port_off "$ch" \
    --on-duration 0.3 \
    --sleep-between 2.0 \
    --off-duration "$hold" \
    --post-off-s "$hold"
