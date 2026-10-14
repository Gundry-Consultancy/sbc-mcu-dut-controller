"""EspBridgeTransport: the host handle for an ``esp-usbip-bridge`` USB/IP server.

A topology host with ``transport: esp-usbip-bridge`` is an ESP32 running the
esp-usbip-bridge firmware. It is a USB/IP *server* like an SSH host, with two
differences the rest of the controller has to know about:

* **No shell.** Inventory, presence and port power go over its HTTP API
  (:class:`~hil_controller.adapters.esp_usbip_bridge.EspUsbipBridgeClient`),
  never ``exec``. ``exec`` exists only so a code path that was not taught about
  bridges fails with a clear non-zero result instead of an ``AttributeError``.
* **Always exported.** There is no ``usbip bind``/``unbind`` step;
  :class:`~hil_controller.adapters.usbip_bridge.UsbipBridge` skips both when the
  server transport sets :attr:`EspBridgeTransport.usbip_exports_always`.

Serial, flashing and MSC can never run on the bridge: the controller attaches
the busid (``usbip attach -r <addr> -b <busid>``) and does that work locally.

Topology fields (all optional except ``addr``)::

    - id: usbip-bridge-s31
      transport: esp-usbip-bridge
      addr: usbip-a1b2c3.local          # USB/IP server (TCP 3240) + HTTP API host
      api_url: http://usbip-a1b2c3.local  # default http://<addr> (port 80)
      token_env: HIL_BRIDGE_TOKEN_S31   # env var holding the bearer token, if one is set

The token itself never lives in the topology or the DB, only the name of the
environment variable (set in ``run/controller.env``) that holds it.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path, PurePosixPath
from typing import Any

from hil_controller.adapters.esp_usbip_bridge import EspUsbipBridgeClient, bridge_base_url
from hil_controller.hosts.base import ExecResult
from hil_controller.hosts.registry import TRANSPORT_ESP_USBIP_BRIDGE

__all__ = [
    "AUX_BRIDGE_PREFIX",
    "TRANSPORT_ESP_USBIP_BRIDGE",
    "EspBridgeTransport",
    "bridge_client_of",
    "bridge_token",
    "resolve_aux_interface",
]

log = logging.getLogger(__name__)


#: ``auxes.interface`` prefix that points an aux (e.g. the I2C strand mux) at a
#: bridge host's HTTP API, sharing its URL and token: ``bridge:<host_id>``.
AUX_BRIDGE_PREFIX = "bridge:"


def bridge_token(token_env: str | None) -> str | None:
    """Read a bridge bearer token from the named environment variable (or None)."""
    if not token_env:
        return None
    return os.environ.get(token_env) or None


class EspBridgeTransport:
    """HostTransport-shaped handle for an esp-usbip-bridge host (HTTP, no shell)."""

    #: Read by UsbipBridge: devices are always exported, skip bind/unbind.
    usbip_exports_always = True

    def __init__(self, *, host_id: str, addr: str, bridge: EspUsbipBridgeClient) -> None:
        self.host_id = host_id
        self.addr = addr
        self.bridge = bridge

    @classmethod
    def from_host(cls, host: dict[str, Any]) -> EspBridgeTransport:
        """Build from a topology/DB host record (``addr``, ``api_url``, ``token_env``)."""
        base_url = bridge_base_url(host.get("addr") or "", host.get("api_url"))
        client = EspUsbipBridgeClient(base_url, token=bridge_token(host.get("token_env")))
        return cls(host_id=str(host.get("id") or ""), addr=host.get("addr") or "", bridge=client)

    async def exec(
        self,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
        cwd: str | None = None,
        on_line: Callable[[str], None] | None = None,
    ) -> ExecResult:
        msg = (
            f"host {self.host_id!r} is an esp-usbip-bridge (HTTP API, no shell); "
            f"cannot run {argv[:1]!r} there — attach the device over usbip and run "
            "it on the controller"
        )
        log.warning(msg)
        return ExecResult(exit_status=127, stdout="", stderr=msg)

    async def stream(self, argv: list[str]) -> AsyncIterator[bytes]:
        raise NotImplementedError(f"host {self.host_id!r} is an esp-usbip-bridge: no shell")
        yield b""  # pragma: no cover - makes this an async generator

    async def copy_to(self, local: Path, remote: PurePosixPath) -> None:
        raise NotImplementedError(f"host {self.host_id!r} is an esp-usbip-bridge: no filesystem")

    async def copy_from(self, remote: PurePosixPath, local: Path) -> None:
        raise NotImplementedError(f"host {self.host_id!r} is an esp-usbip-bridge: no filesystem")

    async def healthcheck(self) -> bool:
        return await self.bridge.ping()


def bridge_client_of(transport: Any) -> EspUsbipBridgeClient | None:
    """The bridge client behind *transport* (also through a recording proxy), or None."""
    client = getattr(transport, "bridge", None)
    return client if isinstance(client, EspUsbipBridgeClient) else None


async def resolve_aux_interface(
    db_path: str, interface: str | None
) -> tuple[str | None, str | None]:
    """Resolve an aux ``interface`` to ``(base_url, token)``.

    A plain URL passes through with no token. ``bridge:<host_id>`` resolves to
    that bridge host's API base URL and its token (from ``token_env``), so an
    analog-mux aux on a bridge shares the host's address and credentials.
    Unknown hosts resolve to ``(None, None)``.
    """
    if not interface or not interface.startswith(AUX_BRIDGE_PREFIX):
        return interface, None
    host_id = interface[len(AUX_BRIDGE_PREFIX) :].strip()
    if not db_path or not host_id:
        return None, None
    from hil_controller.db.connection import get_db

    async with get_db(db_path) as db:
        cur = await db.execute(
            "SELECT addr, api_url, token_env FROM hosts WHERE id = ?", (host_id,)
        )
        row = await cur.fetchone()
    if row is None:
        return None, None
    try:
        base_url = bridge_base_url(row["addr"] or "", row["api_url"])
    except ValueError:
        return None, None
    return base_url, bridge_token(row["token_env"])
