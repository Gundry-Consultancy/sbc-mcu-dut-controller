#!/usr/bin/env python3
"""Solenoid hub CLI — deploy to /opt/hil/ on every USB-server bench host.

Thin argv wrapper around vendor/hil-detection/usb_hub.py's
SolenoidHubController. The controller's SolenoidHubAdapter shells out
to this script via the host transport.

Usage::

    solenoid_hub_cli.py all_off
    solenoid_hub_cli.py port_on  <channel>
    solenoid_hub_cli.py port_off <channel> [--off-duration 1.0] [--post-off-s 0]
    solenoid_hub_cli.py samd51_uf2 <channel>     # double-tap timing

Channel range: 0..7 (MCP23017 port A pins A0..A7).

Deploy:
    sudo install -m 755 solenoid_hub_cli.py /opt/hil/solenoid_hub_cli.py
    # Then ensure the imported usb_hub module is on PYTHONPATH, e.g.:
    sudo ln -sf /home/pi/hil-detection/usb_hub.py /opt/hil/usb_hub.py
    # (or set HIL_USB_HUB_PATH below)

The CLI exits non-zero with a stderr message on any failure so the
calling SolenoidHubAdapter can surface a clean SolenoidHubError.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# Blinka + adafruit-circuitpython-mcp230xx live in a dedicated venv (created with
# --system-site-packages) at /opt/hil/venv — we avoid `pip --break-system-packages`.
# The controller's SolenoidHubAdapter shells out as `python3 /opt/hil/solenoid_hub_cli.py`
# (system python), so re-exec under the venv interpreter if one exists, transparently —
# no controller-side python-path config needed. Falls back to the current interpreter
# when no venv is present (e.g. a host with system-wide Blinka). $HIL_SOLENOID_VENV
# overrides the path; the guard env var prevents an exec loop.
def _maybe_reexec_in_venv() -> None:
    venv_py = os.environ.get("HIL_SOLENOID_VENV", "/opt/hil/venv/bin/python")
    if (
        os.environ.get("_HIL_SOLENOID_VENV_REEXEC") != "1"
        and os.path.isfile(venv_py)
        and os.path.realpath(venv_py) != os.path.realpath(sys.executable)
    ):
        os.environ["_HIL_SOLENOID_VENV_REEXEC"] = "1"
        os.execv(venv_py, [venv_py, os.path.abspath(__file__), *sys.argv[1:]])

# Allow the operator to override where usb_hub.py lives. Default tries
# the standard /opt/hil/ + the hil-detection submodule's expected paths.
_DEFAULT_USB_HUB_SEARCH = [
    "/opt/hil",
    "/home/pi/hil-detection",
    "/home/pi/dev/hil-detection",
]


def _import_usb_hub() -> "type":
    extra = os.environ.get("HIL_USB_HUB_PATH")
    search = [extra, *_DEFAULT_USB_HUB_SEARCH] if extra else _DEFAULT_USB_HUB_SEARCH
    for candidate in search:
        if candidate and Path(candidate, "usb_hub.py").is_file():
            sys.path.insert(0, candidate)
            break
    try:
        from usb_hub import SolenoidHubController  # type: ignore[import-not-found]
    except ImportError as exc:
        print(
            f"ERROR: cannot import usb_hub.SolenoidHubController; "
            f"searched {search}: {exc}",
            file=sys.stderr,
        )
        sys.exit(2)
    return SolenoidHubController


def _validate_channel(value: str) -> int:
    try:
        ch = int(value, 10)
    except ValueError:
        raise argparse.ArgumentTypeError(f"channel must be integer, got {value!r}")
    # MCP23017 has 16 GPIO: port A (0..7) + port B (8..15). Bank A drives the
    # power-latch solenoids; bank B the matching Pico BOOTSEL presses (B = A + 8).
    if not (0 <= ch <= 15):
        raise argparse.ArgumentTypeError(f"channel out of range (0..15): {ch}")
    return ch


def _i2c_address(value: str) -> int:
    """Parse an MCP23017 I2C address (hex ``0x20`` or decimal ``32``)."""
    try:
        addr = int(value, 0)
    except ValueError:
        raise argparse.ArgumentTypeError(f"i2c-address must be an int, got {value!r}")
    if not (0x03 <= addr <= 0x77):
        raise argparse.ArgumentTypeError(f"i2c-address out of range (0x03..0x77): {value!r}")
    return addr


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="solenoid_hub_cli")
    # The Adafruit 8-channel solenoid driver's MCP23017 is at 0x20 by default, but
    # the A0/A1/A2 jumpers can move it (0x20..0x27) — e.g. a host with a different
    # board or two hubs on one bus. Configurable here + via HIL_SOLENOID_I2C_ADDRESS.
    parser.add_argument(
        "--i2c-address",
        type=_i2c_address,
        default=_i2c_address(os.environ.get("HIL_SOLENOID_I2C_ADDRESS", "0x20")),
        help="MCP23017 I2C address (default 0x20 or $HIL_SOLENOID_I2C_ADDRESS)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("all_off", help="Send OFF to every channel")

    p_on = sub.add_parser("port_on", help="Pulse one channel ON")
    p_on.add_argument("channel", type=_validate_channel)
    p_on.add_argument("--on-duration", type=float, default=0.2)

    p_off = sub.add_parser("port_off", help="Send OFF sequence to one channel")
    p_off.add_argument("channel", type=_validate_channel)
    p_off.add_argument("--on-duration", type=float, default=0.2)
    p_off.add_argument("--sleep-between", type=float, default=0.5)
    p_off.add_argument("--off-duration", type=float, default=1.0)
    # Settle after the OFF press to let the port fully depower (capacitors
    # discharge) before anything treats it as "off" — important when the OFF is
    # meant to recover a wedged native-USB board, not just toggle the latch.
    p_off.add_argument("--post-off-s", type=float, default=0.0)

    p_uf2 = sub.add_parser(
        "samd51_uf2",
        help="SAMD51 double-tap reset (sleep_between=0.1, off_duration=0.3)",
    )
    p_uf2.add_argument("channel", type=_validate_channel)
    return parser


def main(argv: list[str] | None = None) -> int:
    _maybe_reexec_in_venv()  # promote into /opt/hil/venv before importing Blinka
    args = _build_parser().parse_args(argv)
    SolenoidHubController = _import_usb_hub()
    hub = SolenoidHubController(i2c_address=args.i2c_address)
    try:
        if args.cmd == "all_off":
            hub.all_off()
        elif args.cmd == "port_on":
            hub.port_on(args.channel, on_duration=args.on_duration)
        elif args.cmd == "port_off":
            hub.port_off(
                args.channel,
                on_duration=args.on_duration,
                sleep_between=args.sleep_between,
                off_duration=args.off_duration,
            )
            if args.post_off_s > 0:
                time.sleep(args.post_off_s)
        elif args.cmd == "samd51_uf2":
            hub.port_off(args.channel, sleep_between=0.1, off_duration=0.3)
        else:
            print(f"ERROR: unknown command {args.cmd!r}", file=sys.stderr)
            return 2
    finally:
        try:
            hub.cleanup()
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: hub cleanup failed: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
