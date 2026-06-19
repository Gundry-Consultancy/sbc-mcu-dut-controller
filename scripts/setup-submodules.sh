#!/usr/bin/env bash
# Configure per-submodule remotes that .gitmodules can't express:
#
#   vendor/protomq           dual-push: origin -> ai-assisted fork AND tyeth/protomq
#   vendor/wippersnapper-arduino  fetch-only `upstream` remote -> adafruit
#
# Run once after `git submodule update --init`. Safe to re-run.

set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

# ── helpers ──────────────────────────────────────────────────────────────────
require_initialised() {
    local path="$1"
    if ! git submodule status "$path" >/dev/null 2>&1; then
        echo "$path is not initialised. Run: git submodule update --init --recursive" >&2
        exit 1
    fi
}

set_origin_dual_push() {
    local sub="$1" primary="$2" mirror="$3"
    # `remote set-url --push` chokes when multiple pushurls already exist.
    # Reset the multi-valued config first, then append both URLs.
    git -C "$sub" config --unset-all remote.origin.pushurl 2>/dev/null || true
    git -C "$sub" config --add remote.origin.pushurl "$primary"
    git -C "$sub" config --add remote.origin.pushurl "$mirror"
    echo "  origin push URLs:"
    git -C "$sub" remote get-url --push --all origin | sed 's/^/    /'
}

ensure_remote() {
    local sub="$1" name="$2" url="$3"
    if git -C "$sub" remote | grep -qxF "$name"; then
        git -C "$sub" remote set-url "$name" "$url"
    else
        git -C "$sub" remote add "$name" "$url"
    fi
    echo "  $name -> $url"
}

# ── vendor/protomq: dual-push to tyeth/protomq ───────────────────────────────
echo "vendor/protomq"
require_initialised vendor/protomq
set_origin_dual_push vendor/protomq \
    https://github.com/tyeth-ai-assisted/protomq.git \
    https://github.com/tyeth/protomq.git

# ── vendor/wippersnapper-arduino: add upstream remote ────────────────────────
echo "vendor/wippersnapper-arduino"
require_initialised vendor/wippersnapper-arduino
ensure_remote vendor/wippersnapper-arduino upstream \
    https://github.com/adafruit/Adafruit_Wippersnapper_Arduino.git

echo
echo "Done."

