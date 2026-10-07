"""Client + power backend for an ESP32 USB/IP bridge (esp-usbip-bridge firmware).

The bridge is a microcontroller, not a Linux box: it exports the USB devices on
its host port over standard USB/IP (TCP 3240) and serves a small HTTP controller
API on port 80. There is no shell, so nothing here goes through ``exec``; every
call is an HTTP request. Contract (esp-usbip-bridge README, "Controller API")::

    GET  /ping                         {"ok": true}
    GET  /api/info                     hostname, board_id, target, version, auth_required, ...
    GET  /api/usb/devices              every exported device (busid = Linux port path)
    GET  /api/usb/devices/{busid}      one device; 404 when absent (presence check)
    GET  /api/usb/hubs                 hubs, power_switching (per-port|ganged|none), ports
    POST /api/ports/{port}/on|off      body {"force": bool}
    POST /api/ports/{port}/cycle       body {"off_ms": int, "force": bool}; runs async

Errors are ``{"ok": false, "error": "..."}`` with 400 (bad input), 401
(token required), 404 (unknown hub/port/device), 409 (hub not per-port switched,
or the port leads to another hub, and ``force`` not set) or 503 (busy/timeout).

busids are Linux-style port paths that stay stable across re-enumeration
(``1-1`` root port, ``1-1.3`` port 3 of the hub on it), so a DUT's
``hub_port_path`` is both its USB/IP busid and the hub port that powers it.

The bridge's I2C strand mux speaks the sbc-dut-analog-mux-api contract, so it is
driven by the existing :class:`~hil_controller.adapters.analog_mux.AnalogMuxAdapter`
pointed at :attr:`EspUsbipBridgeClient.base_url` (see
:func:`hil_controller.hosts.esp_bridge.resolve_aux_interface`).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 10.0

#: ``devices.power_control`` values that select bridge hub-port power. The
#: ``:force`` form passes ``force: true`` so the bridge switches a port on a hub
#: that does not report per-port power switching (ganged/none) — only useful
#: when the operator knows the hub really does switch that port.
POWER_CONTROL_BRIDGE_PORT = "bridge-port"
POWER_CONTROL_BRIDGE_PORT_FORCE = "bridge-port:force"


class EspBridgeError(RuntimeError):
    """A bridge API call failed (transport error or non-2xx response)."""

    def __init__(self, message: str, *, status: int | None = None, error: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.error = error


class EspBridgeAuthError(EspBridgeError):
    """401: the bridge has a token set and the request did not carry it."""


class EspBridgeNotFoundError(EspBridgeError):
    """404: unknown hub, port or device."""


class EspBridgeNotSwitchableError(EspBridgeError):
    """409: the hub is not per-port switched (or the port feeds another hub)."""


class EspBridgeBusyError(EspBridgeError):
    """503: the bridge's USB host is busy or the request timed out."""


_STATUS_ERRORS: dict[int, type[EspBridgeError]] = {
    401: EspBridgeAuthError,
    404: EspBridgeNotFoundError,
    409: EspBridgeNotSwitchableError,
    503: EspBridgeBusyError,
}


def bridge_base_url(addr: str, api_url: str | None = None) -> str:
    """HTTP base URL for a bridge: explicit ``api_url``, else ``http://<addr>``."""
    url = (api_url or "").strip() or (addr or "").strip()
    if not url:
        raise ValueError("esp-usbip-bridge host needs an addr or api_url")
    if "://" not in url:
        url = f"http://{url}"
    return url.rstrip("/")


class EspUsbipBridgeClient:
    """Async client for one bridge's HTTP controller API."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token or None
        self._timeout = timeout_s
        self._client = client  # injectable for tests (httpx.MockTransport)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def _request(self, method: str, path: str, *, json: Any = None) -> Any:
        url = self.base_url + path
        try:
            if self._client is not None:
                resp = await self._client.request(
                    method, url, headers=self._headers(), json=json, timeout=self._timeout
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.request(method, url, headers=self._headers(), json=json)
        except httpx.HTTPError as exc:
            raise EspBridgeError(f"{method} {url} failed: {exc}") from exc
        if resp.status_code >= 400:
            error = ""
            try:
                body = resp.json()
                if isinstance(body, dict):
                    error = str(body.get("error") or "")
            except Exception:  # noqa: BLE001 - a non-JSON error body still maps by status
                error = resp.text[:200]
            cls = _STATUS_ERRORS.get(resp.status_code, EspBridgeError)
            raise cls(
                f"{method} {url} -> HTTP {resp.status_code}: {error or resp.text[:200]}",
                status=resp.status_code,
                error=error,
            )
        try:
            return resp.json()
        except Exception:  # noqa: BLE001 - a 2xx with no/invalid JSON is still success
            return {}

    # ------------------------------------------------------------------ #
    # discovery / health                                                  #
    # ------------------------------------------------------------------ #

    async def ping(self) -> bool:
        """True when ``GET /ping`` answers ``{"ok": true}``; never raises."""
        try:
            body = await self._request("GET", "/ping")
        except EspBridgeError:
            return False
        return bool(isinstance(body, dict) and body.get("ok"))

    async def info(self) -> dict[str, Any]:
        body = await self._request("GET", "/api/info")
        return body if isinstance(body, dict) else {}

    # ------------------------------------------------------------------ #
    # USB devices + hubs                                                  #
    # ------------------------------------------------------------------ #

    async def devices(self) -> list[dict[str, Any]]:
        body = await self._request("GET", "/api/usb/devices")
        return list((body or {}).get("devices") or []) if isinstance(body, dict) else []

    async def device(self, busid: str) -> dict[str, Any] | None:
        """One device, or ``None`` when the bridge answers 404 (not enumerated)."""
        try:
            body = await self._request("GET", f"/api/usb/devices/{busid}")
        except EspBridgeNotFoundError:
            return None
        return body if isinstance(body, dict) else {}

    async def device_present(self, busid: str) -> bool:
        """Presence check: 200 → True, 404 → False; other errors raise."""
        return await self.device(busid) is not None

    async def hubs(self) -> list[dict[str, Any]]:
        body = await self._request("GET", "/api/usb/hubs")
        return list((body or {}).get("hubs") or []) if isinstance(body, dict) else []

    # ------------------------------------------------------------------ #
    # hub port power                                                      #
    # ------------------------------------------------------------------ #

    async def port_on(self, port: str, *, force: bool = False) -> dict[str, Any]:
        return dict(await self._request("POST", f"/api/ports/{port}/on", json={"force": force}))

    async def port_off(self, port: str, *, force: bool = False) -> dict[str, Any]:
        return dict(await self._request("POST", f"/api/ports/{port}/off", json={"force": force}))

    async def port_cycle(
        self, port: str, *, off_ms: int = 1000, force: bool = False
    ) -> dict[str, Any]:
        """Bridge-side off/wait/on. Asynchronous on the bridge: poll presence after."""
        return dict(
            await self._request(
                "POST", f"/api/ports/{port}/cycle", json={"off_ms": int(off_ms), "force": force}
            )
        )


def find_hub_port(
    hubs: list[dict[str, Any]], port_path: str
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """``(hub, port)`` from a ``/api/usb/hubs`` listing whose port path is ``port_path``."""
    for hub in hubs:
        for port in hub.get("ports") or []:
            if port.get("path") == port_path:
                return hub, port
    return None


# --------------------------------------------------------------------------- #
# power_control: bridge-port                                                  #
# --------------------------------------------------------------------------- #


def bridge_power_force(device: dict[str, Any]) -> bool | None:
    """Parse ``device.power_control``: None = not bridge power, else the force flag."""
    value = str(device.get("power_control") or "").strip().lower()
    if value == POWER_CONTROL_BRIDGE_PORT:
        return False
    if value == POWER_CONTROL_BRIDGE_PORT_FORCE:
        return True
    return None


def uses_bridge_port_power(device: dict[str, Any]) -> bool:
    """True when the device is powered via its bridge hub port (and has a port path)."""
    return bridge_power_force(device) is not None and bool(device.get("hub_port_path"))


class BridgePortPower:
    """Power one DUT through the bridge hub port that feeds it.

    ``port`` is the DUT's ``hub_port_path`` — on the bridge the port path and the
    device busid are the same string. Presence is ``GET /api/usb/devices/{busid}``
    (200 present / 404 absent), the bridge equivalent of the SSH hosts'
    ``test -e /sys/bus/usb/devices/<busid>``.

    A 409 from the bridge means the hub does not report per-port power switching
    (many cheap hubs are ganged, e.g. GL850G) or the port feeds another hub; it is
    re-raised as :class:`EspBridgeNotSwitchableError` with a hint to opt in to
    ``power_control: bridge-port:force`` only if the hub really switches it.
    """

    def __init__(
        self,
        client: EspUsbipBridgeClient,
        port: str,
        *,
        force: bool = False,
        poll_s: float = 0.5,
    ) -> None:
        self.client = client
        self.port = port
        self.force = force
        self.poll_s = poll_s

    @property
    def label(self) -> str:
        return f"bridge port {self.port}" + (" (force)" if self.force else "")

    def _explain(self, exc: EspBridgeNotSwitchableError, action: str) -> EspBridgeError:
        return EspBridgeNotSwitchableError(
            f"bridge refused power {action} on port {self.port} (409: "
            f"{exc.error or exc}); the hub is not per-port switched or the port feeds "
            "another hub. Use a per-port switched hub, or set power_control: "
            "bridge-port:force only if this hub really switches the port.",
            status=exc.status,
            error=exc.error,
        )

    async def on(self) -> None:
        try:
            await self.client.port_on(self.port, force=self.force)
        except EspBridgeNotSwitchableError as exc:
            raise self._explain(exc, "on") from exc

    async def off(self) -> None:
        try:
            await self.client.port_off(self.port, force=self.force)
        except EspBridgeNotSwitchableError as exc:
            raise self._explain(exc, "off") from exc

    async def present(self) -> bool:
        """Presence of the DUT on the bridge; an unreachable bridge reads as absent."""
        try:
            return await self.client.device_present(self.port)
        except EspBridgeError as exc:
            log.debug("bridge presence probe for %s failed: %s", self.port, exc)
            return False

    async def await_presence(self, present: bool, *, timeout_s: float) -> bool:
        """Poll until the DUT's presence equals ``present`` or ``timeout_s`` elapses."""
        deadline = time.monotonic() + timeout_s
        while True:
            if await self.present() == present:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(self.poll_s)

    async def power_cycle(
        self,
        *,
        off_s: float = 1.0,
        settle_s: float = 2.0,
        disappear_timeout_s: float = 10.0,
        reappear_timeout_s: float = 30.0,
        on_line: Callable[[str], None] | None = None,
    ) -> bool:
        """Detection-driven cold boot: off → await gone → (off_s) → on → await back.

        Mirrors the solenoid ``power_cycle`` stage's defaults (off 1 s, disappear
        ≤10 s, reappear ≤30 s, settle 2 s). Returns True when the DUT
        re-enumerated; False (with a warning line) when it did not.
        """

        def _say(msg: str) -> None:
            if on_line is not None:
                on_line(msg)

        was_present = await self.present()
        _say(f"power-cycle {self.label}: device {'present' if was_present else 'absent'} pre-cycle")
        started = time.monotonic()
        await self.off()
        if was_present:
            if await self.await_presence(False, timeout_s=disappear_timeout_s):
                _say(f"device disappeared after power-off (within {disappear_timeout_s:.0f}s)")
            else:
                _say(
                    f"WARNING: device still enumerated {disappear_timeout_s:.0f}s after "
                    "power-off — the port may not really be switched"
                )
        remaining_off = off_s - (time.monotonic() - started)
        if remaining_off > 0:
            await asyncio.sleep(remaining_off)
        await self.on()
        if await self.await_presence(True, timeout_s=reappear_timeout_s):
            _say(f"device re-enumerated after power-on (within {reappear_timeout_s:.0f}s)")
            if settle_s > 0:
                await asyncio.sleep(settle_s)
            return True
        _say(f"WARNING: device did not re-enumerate within {reappear_timeout_s:.0f}s of power-on")
        return False
