#!/usr/bin/env python3
"""CLI for the WipperSnapper version-bisection engine (hil_controller.bisect).

Find the first WS-Arduino release where a board broke, by flashing+testing the
releases between a working and a broken ref on real HIL hardware.

Usage (controller URL + token + Adafruit IO / WiFi secrets come from the env):

    export HIL_BASE_URL=http://tachyon-….ts.net:8080
    export HIL_TOKEN=…              # controller bearer token
    export IO_USERNAME=… IO_KEY=… WIFI_SSID=… WIFI_PASSWORD=…
    export GITHUB_TOKEN=…           # optional, lifts GitHub API rate limits

    python scripts/hil_bisect.py \
        --device mcu-pyportal \
        --working-ref 1.0.0-beta.78 \
        --broken-ref  1.0.0-beta.128 \
        --asset-glob '*pyportal_titano_tinyusb*.uf2'

Exit code 0 = a boundary was found; non-zero = a precondition failed (bad oracle,
both-versions-passed, unflashable target, …) — the message says which.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from hil_controller.bisect import WS_REPO, BisectConfig, BisectError, BisectRunner

# Known board → release-asset glob (extend as boards are enrolled). Falls back to
# --asset-glob when a device isn't listed here.
DEVICE_ASSET_GLOB = {
    "mcu-pyportal": "*pyportal_titano_tinyusb*.uf2",
}


def _secrets_from_env() -> dict[str, str]:
    out = {}
    for k in ("IO_USERNAME", "IO_KEY", "WIFI_SSID", "WIFI_PASSWORD"):
        v = os.environ.get(k)
        if v:
            out[k] = v
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="WipperSnapper release version-bisection")
    p.add_argument("--device", required=True, help="controller device id, e.g. mcu-pyportal")
    p.add_argument("--working-ref", required=True, help="known-good release tag")
    p.add_argument("--broken-ref", required=True, help="known-broken release tag")
    p.add_argument("--asset-glob", default="", help="release-asset fnmatch (per-board firmware)")
    p.add_argument("--repo", default=WS_REPO)
    p.add_argument("--flasher", default="uf2-msc")
    p.add_argument(
        "--io-url",
        default="io.adafruit.com",
        help="broker the DUT checks in to (blank = local per-session broker, anonymous creds)",
    )
    p.add_argument("--io-port", type=int, default=8883)
    p.add_argument("--verify-times", type=int, default=2, help="test each version N times")
    p.add_argument("--infra-retries", type=int, default=2)
    p.add_argument("--job-timeout-s", type=float, default=900.0)
    p.add_argument("--window-minutes", type=int, default=2)
    p.add_argument("--base-url", default=os.environ.get("HIL_BASE_URL", ""))
    p.add_argument("--token", default=os.environ.get("HIL_TOKEN", ""))
    args = p.parse_args(argv)

    if not args.base_url or not args.token:
        p.error("controller --base-url/--token (or HIL_BASE_URL/HIL_TOKEN) are required")
    asset_glob = args.asset_glob or DEVICE_ASSET_GLOB.get(args.device, "")
    if not asset_glob:
        p.error(f"no --asset-glob and no default for device {args.device!r}")
    secrets = _secrets_from_env()
    # Drop a placeholder IO key so the server either anon-derives (local broker)
    # or fails fast (cloud) instead of flashing a board that reboot-loops on auth.
    from hil_controller.bisect import is_cloud_broker, is_real_io_key

    if secrets.get("IO_KEY") and not is_real_io_key(secrets["IO_KEY"]):
        secrets.pop("IO_KEY", None)
        secrets.pop("IO_USERNAME", None)
    if is_cloud_broker(args.io_url) and not is_real_io_key(secrets.get("IO_KEY", "")):
        print(
            f"warning: cloud broker {args.io_url!r} needs a real IO_USERNAME/IO_KEY in env — "
            "the checkin will FAIL (or pass --io-url '' for the anonymous local broker)",
            file=sys.stderr,
        )

    cfg = BisectConfig(
        device_id=args.device,
        working_ref=args.working_ref,
        broken_ref=args.broken_ref,
        asset_glob=asset_glob,
        base_url=args.base_url.rstrip("/"),
        token=args.token,
        repo=args.repo,
        flasher=args.flasher,
        secrets=secrets,
        io_url=args.io_url,
        io_port=args.io_port,
        gh_token=os.environ.get("GITHUB_TOKEN", ""),
        verify_times=args.verify_times,
        infra_retries=args.infra_retries,
        job_timeout_s=args.job_timeout_s,
        window_minutes=args.window_minutes,
    )
    runner = BisectRunner(cfg, log=lambda m: print(m, flush=True))
    try:
        result = runner.run()
    except BisectError as exc:
        print(f"\nBISECT FAILED: {exc}", file=sys.stderr)
        return 2
    print("\n" + json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
