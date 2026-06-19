"""SolenoidHubAdapter — async facade over the MCP23017 hub-control helper.

The Adafruit 8-channel solenoid driver (product #6318) at I²C ``0x20`` on
``rpi-displays`` powers each USB hub port's soft-latching button. The
synchronous driver lives in ``vendor/hil-detection/usb_hub.py``; this
adapter calls a small CLI wrapper around it (``solenoid_hub_cli.py``
deployed to ``/opt/hil/`` on the bench) over a :class:`HostTransport`.

Public surface mirrors what :class:`UsbFingerprintAdapter` already
expects in its ``hub`` provider slot, so wiring this in finally turns
``/v1/devices/{id}/learn-usb`` from a no-op into a real depower/repower
capture::

    async def all_off()
    async def port_on(channel)
    async def port_off(channel, *, hold_s=1.0)
    async def samd51_uf2(channel)       # double-tap timings

Channel numbering matches ``device.solenoid_channel`` from topology YAML
(0..7). All eight channels are operational per OQ9 stakeholder
directive — the adapter does not gate writes on a per-channel "known
good" flag.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# Standard absolute path to the CLI helper on every bench host. Override
# via :class:`SolenoidHubAdapter`(cli_path=...) for non-standard layouts
# (e.g. running the cli straight out of a dev checkout).
DEFAULT_CLI_PATH = "/opt/hil/solenoid_hub_cli.py"


class SolenoidHubError(RuntimeError):
    """The hub CLI call failed."""


def _channel_arg(channel: int) -> str:
    """Stringify a channel index, with a validity check (0..7)."""
    if not (0 <= channel <= 7):
        raise ValueError(f"solenoid channel out of range (0..7): {channel}")
    return str(channel)


class SolenoidHubAdapter:
    """Run the solenoid hub CLI on the host that owns the MCP23017."""

    def __init__(
        self,
        *,
        transport: Any,
        cli_path: str = DEFAULT_CLI_PATH,
        python: str = "python3",
        sudo: bool = False,
    ) -> None:
        self.transport = transport
        self.cli_path = cli_path
        self.python = python
        self._sudo = sudo

    # ------------------------------------------------------------------ #
    # Internal                                                            #
    # ------------------------------------------------------------------ #

    def _argv(self, *args: str) -> list[str]:
        base: list[str] = []
        if self._sudo:
            base.append("sudo")
        base.extend([self.python, self.cli_path])
        base.extend(args)
        return base

    async def _call(self, *args: str, what: str) -> str:
        argv = self._argv(*args)
        result = await self.transport.exec(argv)
        if result.exit_status != 0:
            raise SolenoidHubError(
                f"solenoid {what} failed (exit {result.exit_status}): "
                f"{(result.stderr or result.stdout or '').strip()[:200]}"
            )
        return result.stdout or ""

    # ------------------------------------------------------------------ #
    # Public API — matches the UsbFingerprintAdapter `hub` contract       #
    # ------------------------------------------------------------------ #

    async def all_off(self) -> None:
        """Send the OFF sequence to every hub port."""
        await self._call("all_off", what="all_off")

    async def port_on(self, channel: int) -> None:
        """Pulse the channel's button to bring the hub port up."""
        await self._call("port_on", _channel_arg(channel), what=f"port_on({channel})")

    async def port_off(
        self,
        channel: int,
        *,
        hold_s: float = 1.0,
        post_off_s: float = 0.0,
        presses: int = 2,
        gap_s: float = 0.12,
    ) -> None:
        """Send the OFF sequence; ``hold_s`` overrides the long OFF pulse.

        A latching solenoid can miss a single pulse, so the OFF is pressed
        ``presses`` times (default **2**) with a ``gap_s`` (default **120 ms**)
        gap — reliable single-channel actuation. ``post_off_s`` adds a settle
        *after the final* OFF press so the port fully depowers (capacitors
        discharge) before it's treated as off — important when the OFF is meant
        to recover a wedged native-USB board.
        """
        import asyncio

        n = max(1, presses)
        for i in range(n):
            if i:
                await asyncio.sleep(gap_s)
            args = ["port_off", _channel_arg(channel), "--off-duration", str(hold_s)]
            if post_off_s > 0 and i == n - 1:  # depower settle only after the last press
                args += ["--post-off-s", str(post_off_s)]
            await self._call(*args, what=f"port_off({channel}) press {i + 1}/{n}")

    async def samd51_uf2(self, channel: int) -> None:
        """SAMD51 double-tap: short ON, ~100 ms gap, ~300 ms OFF pulse."""
        await self._call(
            "samd51_uf2",
            _channel_arg(channel),
            what=f"samd51_uf2({channel})",
        )

    async def power_cycle(
        self,
        channel: int,
        *,
        off_s: float = 1.0,
        settle_s: float = 0.0,
        post_off_s: float = 0.0,
    ) -> None:
        """Convenience: ``port_off`` then ``port_on`` (with optional settle).

        ``post_off_s`` waits after the OFF press for the port to depower before
        powering back on (a true cold boot); ``settle_s`` waits after ON for the
        device to come up. ``UsbFingerprintAdapter`` does this manually today;
        the helper exists so test scripts and the manual-reset UI endpoint can
        call it.
        """
        import asyncio

        await self.port_off(channel, hold_s=off_s, post_off_s=post_off_s)
        if settle_s > 0:
            await asyncio.sleep(settle_s)
        await self.port_on(channel)
