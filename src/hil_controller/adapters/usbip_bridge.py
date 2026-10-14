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

An **esp-usbip-bridge** server (``transport: esp-usbip-bridge``, see
``hosts/esp_bridge.py``) always exports its devices and has no shell, so bind
and unbind are skipped for it: the server transport advertises this with
``usbip_exports_always = True``. Everything on the client side is unchanged.
Because a bridge keeps exporting a device across re-enumeration while the
client's attachment drops whenever the device disconnects (power-cycle,
1200-baud touch into a bootloader), :class:`UsbipAttachKeeper` can hold a
busid attached for the length of a job.

If the server goes away (a bridge reboot) while a device is attached, the
client's vhci port can stay marked in use with no USB device behind it. From
then on every ``usbip`` command fails with "open vhci_driver (is vhci_hcd
loaded?)", ``usbip detach`` included. :meth:`UsbipBridge.attach` and
:meth:`UsbipBridge.detach` recognise that error, clear the stale port(s)
through ``/sys/devices/platform/vhci_hcd.0/detach`` and retry once.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# A `usbip port` block opens with e.g. "Port 03: <Port in Use> ...".
_PORT_RE = re.compile(r"^\s*Port\s+(\d+):", re.IGNORECASE)

#: vhci_hcd sysfs: ``status`` (+ ``status.N`` per extra controller, global port
#: numbers) and the ``detach`` attribute, both on the first platform device.
VHCI_SYSFS = "/sys/devices/platform/vhci_hcd.0"
#: Where ``usbip attach`` records each port's remote ("<host> <port> <busid>").
VHCI_RECORDS = "/var/run/vhci_hcd"
#: vhci port status for a free port (VDEV_ST_NULL).
VHCI_ST_NULL = 4

# libusbip can't resolve a used port's device in sysfs → every command fails.
_VHCI_DRIVER_ERROR = "open vhci_driver"

# One exec on the client gathers everything the stale-port check needs.
_VHCI_PROBE = (
    f"cat {VHCI_SYSFS}/status* 2>/dev/null; echo '--records--'; "
    f'for f in {VHCI_RECORDS}/port*; do [ -f "$f" ] && echo "${{f##*/port}} $(cat "$f")"; done; '
    "echo '--devices--'; ls -1 /sys/bus/usb/devices 2>/dev/null; true"
)


def is_vhci_driver_error(text: str | None) -> bool:
    """True for libusbip's "open vhci_driver (is vhci_hcd loaded?)" failure."""
    return _VHCI_DRIVER_ERROR in (text or "")


@dataclass(frozen=True)
class VhciPort:
    port: int
    status: int
    local_busid: str
    remote_host: str | None = None
    remote_busid: str | None = None


def parse_vhci_probe(text: str) -> tuple[list[VhciPort], set[str]]:
    """Parse :data:`_VHCI_PROBE` output into the vhci ports and the USB devices
    present in sysfs.

    Status lines look like ``hs  0000 006 002 00010078 000003 1-1`` (hub, port,
    sta, spd, dev, sockfd, local_busid); record lines ``<port> <host> <tcp port>
    <busid>``.
    """
    section = "status"
    rows: list[tuple[int, int, str]] = []
    records: dict[int, tuple[str, str]] = {}
    devices: set[str] = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line in ("--records--", "--devices--"):
            section = line.strip("-")
            continue
        parts = line.split()
        if section == "status":
            if len(parts) >= 7 and parts[1].isdigit() and parts[2].isdigit():
                rows.append((int(parts[1]), int(parts[2]), parts[6]))
        elif section == "records":
            if len(parts) >= 4 and parts[0].isdigit():
                records[int(parts[0])] = (parts[1], parts[3])
        elif line:
            devices.add(line)
    ports = [
        VhciPort(port, sta, busid, *records.get(port, (None, None))) for port, sta, busid in rows
    ]
    return ports, devices


def stale_vhci_ports(
    ports: Iterable[VhciPort], devices: set[str], *, server_addr: str, busid: str
) -> list[int]:
    """Ports to clear when ``usbip`` fails with the vhci_driver error.

    Only two kinds of port are touched, so other jobs' attachments survive:

    - a used port whose local busid has no ``/sys/bus/usb/devices`` entry: this
      is what breaks libusbip for every caller, whatever server it pointed at;
    - a used port recorded as *this* server's *busid*: our own earlier
      attachment, which is about to be replaced by a new attach anyway.
    """
    stale = []
    for p in ports:
        if p.status == VHCI_ST_NULL:
            continue
        orphaned = p.local_busid not in devices
        ours = p.remote_host == server_addr and p.remote_busid == busid
        if orphaned or ours:
            stale.append(p.port)
    return stale


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
        bind_required: bool | None = None,
    ) -> None:
        self.server_tp = server_tp
        self.client_tp = client_tp
        self.server_addr = server_addr
        self.busid = busid
        self._sudo = sudo
        self.settle_s = settle_s
        # ``is True`` (not truthiness): a MagicMock/AsyncMock server transport
        # would otherwise answer any attribute with a truthy mock.
        if bind_required is None:
            bind_required = getattr(server_tp, "usbip_exports_always", False) is not True
        self.bind_required = bind_required

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
        if not self.bind_required:
            log.debug("usbip bind skipped for %s: server always exports", self.busid)
            return
        await self._run(
            self.server_tp, self._argv("usbip", "bind", "-b", self.busid), what="usbip bind"
        )

    async def unbind(self, *, check: bool = True) -> None:
        if not self.bind_required:
            return
        await self._run(
            self.server_tp,
            self._argv("usbip", "unbind", "-b", self.busid),
            what="usbip unbind",
            check=check,
        )

    async def clear_stale_vhci_ports(self) -> list[int]:
        """Detach stale vhci ports through sysfs (see :func:`stale_vhci_ports`).

        Returns the ports cleared. ``usbip detach`` can't do this: it fails with
        the same vhci_driver error as every other usbip command.
        """
        probe = await self.client_tp.exec(self._argv("bash", "-c", _VHCI_PROBE))
        ports, devices = parse_vhci_probe(probe.stdout or "")
        stale = stale_vhci_ports(ports, devices, server_addr=self.server_addr, busid=self.busid)
        cleared = []
        for port in stale:
            result = await self._run(
                self.client_tp,
                self._argv("sh", "-c", f"echo {port} > {VHCI_SYSFS}/detach"),
                what=f"vhci detach port {port}",
                check=False,
            )
            if result.exit_status == 0:
                cleared.append(port)
            else:
                log.warning(
                    "vhci: clearing stale port %d failed: %s", port, (result.stderr or "").strip()
                )
        if cleared:
            log.warning(
                "vhci: cleared stale port(s) %s (usbip failed with 'open vhci_driver')", cleared
            )
        return cleared

    async def _run_usbip(self, argv: list[str], *, what: str, check: bool) -> Any:
        """Run a client-side usbip command; on the vhci_driver error, clear the
        stale vhci port(s) and retry once."""
        result = await self._run(self.client_tp, argv, what=what, check=False)
        if result.exit_status != 0 and is_vhci_driver_error(result.stderr):
            if await self.clear_stale_vhci_ports():
                result = await self._run(self.client_tp, argv, what=what, check=False)
        if check and result.exit_status != 0:
            raise RuntimeError(f"{what} failed (exit {result.exit_status}): {result.stderr}")
        return result

    async def attach(self, *, check: bool = True) -> Any:
        return await self._run_usbip(
            self._argv("usbip", "attach", "-r", self.server_addr, "-b", self.busid),
            what="usbip attach",
            check=check,
        )

    async def is_attached(self) -> bool:
        """True when ``usbip port`` on the client lists this busid."""
        result = await self._run(
            self.client_tp, self._argv("usbip", "port"), what="usbip port", check=False
        )
        return parse_usbip_port(result.stdout or "", self.busid) is not None

    async def detach(self, *, check: bool = True) -> None:
        result = await self._run_usbip(self._argv("usbip", "port"), what="usbip port", check=False)
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
    async def attached(self) -> AsyncGenerator[str | None, None]:
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


class UsbipAttachKeeper:
    """Hold a busid attached on the client for a whole job (bridge servers).

    The client's vhci attachment ends whenever the device disconnects on the
    server — a power-cycle, or a 1200-baud touch that re-enumerates the board
    into its bootloader. An esp-usbip-bridge keeps exporting the busid (same
    port path) after re-enumeration, so the keeper simply re-runs
    ``usbip attach`` whenever the busid is missing from ``usbip port`` and the
    device is present on the server (``present``, e.g. the bridge's
    ``GET /api/usb/devices/{busid}``). Polls every ``poll_s``.
    """

    def __init__(
        self,
        bridge: UsbipBridge,
        *,
        present: Callable[[], Awaitable[bool]] | None = None,
        poll_s: float = 1.0,
        on_line: Callable[[str], None] | None = None,
    ) -> None:
        self.bridge = bridge
        self.present = present
        self.poll_s = poll_s
        self.on_line = on_line
        self.reattach_count = 0
        self._task: asyncio.Task[None] | None = None

    def _say(self, msg: str) -> None:
        log.info("usbip keeper: %s", msg)
        if self.on_line is not None:
            try:
                self.on_line(msg)
            except Exception:  # noqa: BLE001 - a log sink must never break the keeper
                log.warning("usbip keeper log sink raised", exc_info=True)

    async def ensure_attached(self) -> bool:
        """Attach now if needed; True when the busid is attached afterwards."""
        if await self.bridge.is_attached():
            return True
        if self.present is not None and not await self.present():
            return False
        result = await self.bridge.attach(check=False)
        if getattr(result, "exit_status", 1) != 0:
            self._say(
                f"usbip attach {self.bridge.busid} from {self.bridge.server_addr} failed: "
                f"{(getattr(result, 'stderr', '') or '').strip()[:200]}"
            )
            return False
        self.reattach_count += 1
        self._say(f"attached {self.bridge.busid} from {self.bridge.server_addr}")
        return True

    async def _loop(self) -> None:
        while True:
            try:
                await self.ensure_attached()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep holding; next poll retries
                log.warning("usbip keeper poll failed for %s: %s", self.bridge.busid, exc)
            await asyncio.sleep(self.poll_s)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._loop(), name=f"usbip-keeper-{self.bridge.busid}"
            )

    async def stop(self, *, detach: bool = True) -> None:
        """Stop polling and (by default) detach the busid from the client."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        if detach:
            try:
                await self.bridge.detach(check=False)
            except Exception as exc:  # noqa: BLE001 - best-effort teardown
                log.warning("usbip detach failed during keeper stop: %s", exc)
