#!/usr/bin/env python3
"""Add a libcamera AF tuning block to imx519.json.

The IMX519 sensor tuning file shipped with Pi OS (and upstream
libcamera) omits the ``rpi.af`` algorithm block — even though Arducam's
common IMX519 modules have a working VCM motor and the IPA library has
the AF algorithm compiled in. Without the tuning block libcamera
rejects AfMode/LensPosition with "no AF algorithm" warnings.

This script grafts a reasonable AF block in place. The lens position
map is calibrated for the Arducam IMX519 16MP autofocus module
(0..32 dioptre range, linear mapping to a 10-bit VCM register).

Run once on each Pi CSI host with an IMX519 camera:

    sudo python3 imx519-af-patch.py

Idempotent — re-running replaces any existing rpi.af block. A copy of
the original is saved alongside as imx519.json.preaf the first time.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

DEFAULT_PATH = Path("/usr/share/libcamera/ipa/rpi/vc4/imx519.json")

AF_BLOCK = {
    "rpi.af": {
        "ranges": {
            "normal": {"min": 0.0, "max": 12.0, "default": 1.0},
            "macro": {"min": 3.0, "max": 32.0, "default": 6.0},
            "full": {"min": 0.0, "max": 32.0, "default": 1.0},
        },
        "speeds": {
            "normal": {
                "step_coarse": 1.0,
                "step_fine": 0.25,
                "contrast_ratio": 0.75,
                "retrigger_ratio": 0.8,
                "retrigger_delay": 10,
                "max_slew": 1.5,
                "dropout_frames": 6,
                "step_frames": 5,
            },
            "fast": {
                "step_coarse": 2.0,
                "step_fine": 0.5,
                "contrast_ratio": 0.75,
                "retrigger_ratio": 0.8,
                "retrigger_delay": 8,
                "max_slew": 3.0,
                "dropout_frames": 4,
                "step_frames": 4,
            },
        },
        "conf_epsilon": 8,
        "conf_thresh": 16,
        "conf_clip": 512,
        "skip_frames": 5,
        # Dioptre 0.0 -> VCM register 0, dioptre 32.0 -> VCM register 1023.
        # Linear mapping that matches Arducam IMX519 AF module behaviour.
        "map": [0.0, 0, 32.0, 1023],
    }
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_PATH,
        help=f"tuning file to patch (default: {DEFAULT_PATH})",
    )
    args = ap.parse_args()

    if not args.path.exists():
        print(f"ERROR: {args.path} not found", file=sys.stderr)
        return 2

    backup = args.path.with_suffix(args.path.suffix + ".preaf")
    if not backup.exists():
        shutil.copy2(args.path, backup)
        print(f"backup written: {backup}")

    with args.path.open() as f:
        data = json.load(f)

    algorithms = data.get("algorithms", [])
    algorithms = [a for a in algorithms if "rpi.af" not in a]
    algorithms.append(AF_BLOCK)
    data["algorithms"] = algorithms

    with args.path.open("w") as f:
        json.dump(data, f, indent=4)
    print(f"AF block grafted into {args.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
