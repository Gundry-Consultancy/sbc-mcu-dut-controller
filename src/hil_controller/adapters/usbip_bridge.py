"""usbip bridge: broker a USB device from a server host onto a client host.

Used by per-phase execution-location for arduino-ws jobs (and, later, the
usbip port-leasing work). The *server* is the host physically holding the
device (e.g. rpi-displays); the *client* is where flashing runs (e.g. the
controller Tachyon, reached via ``LocalTransport``).

Lifecycle, via the :meth:`UsbipBridge.attached` async context manager::

    ensure vhci-hcd (client) → bind (server) → attach (client)
        → yield the freshly-enumerated /dev/tty* on the client
    → detach (client) → unbind (server)   [always, even on error]

All usbip / modprobe invocations go through ``transport.exec`` prefixed with
``sudo`` (a passwordless sudoers drop-in is provisioned by setup-hil-host.sh).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

log = logging.getLogger(__name__)

# A `usbip port` block opens with e.g. "Port 03: <Port in Use> ...".
_PORT_RE = re.compile(r"^\s*Port\s+(\d+):", re.IGNORECASE)


def diff_serial_ports(before: list[str], after: list[str]) -> str | None:
    """Return the single serial device that appeared between two listings.

    Robust to naming (``/dev/ttyACM*`` vs ``/dev/ttyUSB*``): we diff the sets
    rather than guess the name. If zero or several appeared, returns the first
    new one (sorted) or ``None`` — callers treat ``None`` as "discovery failed".
    """
    new = sorted(set(after) - set(before))
    return new[0] if new else None


def parse_usbip_port(text: str, busid: str) -> int | None:
    """Find the local vhci port number that a remote *busid* is attached to.

    Scans ``usbip port`` output, tracking the current ``Port NN:`` header and
    returning ``NN`` for the block whose body references ``busid``.
    """
    current: int | None = None
    for line in (text or "").splitlines():
        m = _PORT_RE.match(line)
        if m:
            current = int(m.group(1))
            continue
        if current is not None and busid in line:
            return current
    return None


class UsbipBridge:
    def __init__(
        self,
        *,
        server_tp: Any,
        client_tp: Any,
        server_addr: str,
        busid: str,
        sudo: bool = True,
        settle_s: float = 2.0,
    ) -> None:
        self.server_tp = server_tp
        self.client_tp = client_tp
        self.server_addr = server_addr
        self.busid = busid
        self._sudo = sudo
        self.settle_s = settle_s

    # ------------------------------------------------------------------ #
    # primitives                                                          #
    # ------------------------------------------------------------------ #

    def _argv(self, *args: str) -> list[str]:
        return (["sudo"] if self._sudo else []) + list(args)

    async def _run(self, tp: Any, argv: list[str], *, what: str, check: bool = True) -> Any:
        result = await tp.exec(argv)
        if check and result.exit_status != 0:
            raise RuntimeError(f"{what} failed (exit {result.exit_status}): {result.stderr}")
        return result

    async def ensure_vhci(self) -> None:
        await self._run(
            self.client_tp, self._argv("modprobe", "vhci-hcd"), what="modprobe vhci-hcd"
        )

    async def bind(self) -> None:
        await self._run(
            self.server_tp, self._argv("usbip", "bind", "-b", self.busid), what="usbip bind"
        )

    async def unbind(self, *, check: bool = True) -> None:
        await self._run(
            self.server_tp,
            self._argv("usbip", "unbind", "-b", self.busid),
            what="usbip unbind",
            check=check,
        )

    async def attach(self) -> None:
        await self._run(
            self.client_tp,
            self._argv("usbip", "attach", "-r", self.server_addr, "-b", self.busid),
            what="usbip attach",
        )

    async def detach(self, *, check: bool = True) -> None:
        result = await self._run(
            self.client_tp, self._argv("usbip", "port"), what="usbip port", check=False
        )
        port = parse_usbip_port(result.stdout, self.busid)
        if port is None:
            log.warning("usbip detach: no attached port found for busid %s", self.busid)
            return
        await self._run(
            self.client_tp,
            self._argv("usbip", "detach", "-p", f"{port:02d}"),
            what="usbip detach",
            check=check,
        )

    async def list_serial_ports(self) -> list[str]:
        """List candidate serial devices on the *client* (best-effort)."""
        result = await self.client_tp.exec(
            ["bash", "-c", "ls -1 /dev/ttyACM* /dev/ttyUSB* 2>/dev/null || true"]
        )
        return [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]

    # ------------------------------------------------------------------ #
    # orchestration                                                       #
    # ------------------------------------------------------------------ #

    @asynccontextmanager
    async def attached(self) -> AsyncIterator[str | None]:
        """Bind+attach the device, yield its new serial port, then tear down.

        Teardown (detach + unbind) runs in a ``finally`` so a crash mid-flash
        never leaves the busid bound — the next job can still claim the port.
        """
        ports_before = await self.list_serial_ports()
        await self.ensure_vhci()
        await self.bind()
        try:
            await self.attach()
            if self.settle_s:
                await asyncio.sleep(self.settle_s)
            ports_after = await self.list_serial_ports()
            port = diff_serial_ports(ports_before, ports_after)
            if port is None:
                log.warning("usbip attach: no new serial port appeared for %s", self.busid)
            yield port
        finally:
            try:
                await self.detach(check=False)
            except Exception as exc:  # best-effort teardown
                log.warning("usbip detach failed during teardown: %s", exc)
            try:
                await self.unbind(check=False)
            except Exception as exc:
                log.warning("usbip unbind failed during teardown: %s", exc)
