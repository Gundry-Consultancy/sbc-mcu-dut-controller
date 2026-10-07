"""In-memory fake of the esp-usbip-bridge HTTP controller API, for httpx.MockTransport.

Models just enough of the firmware for controller tests: a device table keyed by
busid (= hub port path), hubs with a ``power_switching`` mode, per-port power
that removes/restores the device behind it, the 409 rule for non per-port hubs,
and optional bearer-token auth on non-GET requests.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from hil_controller.adapters.esp_usbip_bridge import EspUsbipBridgeClient

BASE_URL = "http://usbip-test.local"


def qtpy(busid: str = "1-1.1") -> dict[str, Any]:
    return {
        "busid": busid,
        "vid": "239a",
        "pid": "8143",
        "description": "Adafruit : QT Py ESP32-S3 (239a:8143)",
        "manufacturer": "Adafruit",
        "product": "QT Py ESP32-S3",
        "serial": "7C9E1234",
        "name": "qtpy-s3",
        "speed": "full",
        "speed_mbps": 12,
        "max_power_ma": 100,
        "device_class": "0xef",
        "bcd_device": "0x0100",
        "num_interfaces": 2,
        "interfaces": [{"class": 2, "subclass": 2, "protocol": 0}],
        "virtual": False,
        "address": 3,
        "hub": "1-1",
        "port": 1,
        "port_power_status": "on",
        "port_connect_status": "connected",
    }


class FakeBridge:
    def __init__(self, *, switching: str = "per-port", token: str | None = None) -> None:
        self.switching = switching
        self.token = token
        self.calls: list[tuple[str, str, Any]] = []
        self.devices: dict[str, dict[str, Any]] = {"1-1.1": qtpy("1-1.1")}
        self.parked: dict[str, dict[str, Any]] = {}  # devices behind a powered-off port
        self.power: dict[str, bool] = {"1-1.1": True, "1-1.2": True}
        self.fail_status: int | None = None  # force every request to this status

    # ------------------------------------------------------------------ #
    def client(self) -> EspUsbipBridgeClient:
        return EspUsbipBridgeClient(
            BASE_URL,
            token=self.token,
            client=httpx.AsyncClient(transport=httpx.MockTransport(self.handle)),
        )

    def _hub(self) -> dict[str, Any]:
        ports = []
        for i, path in enumerate(sorted(self.power), start=1):
            dev = self.devices.get(path)
            ports.append(
                {
                    "port": i,
                    "path": path,
                    "power": "on" if self.power[path] else "off",
                    "connected": dev is not None,
                    "enabled": dev is not None,
                    "suspended": False,
                    "over_current": False,
                    "user_off": not self.power[path],
                    "speed": "full" if dev else None,
                    "device": (
                        {"type": "device", "busid": path, "vid": dev["vid"], "pid": dev["pid"]}
                        if dev
                        else None
                    ),
                }
            )
        return {
            "path": "1-1",
            "address": 1,
            "vid": "1a40",
            "pid": "0101",
            "manufacturer": "Terminus",
            "product": "USB 2.0 Hub",
            "ready": True,
            "num_ports": len(ports),
            "power_switching": self.switching,
            "pwr_on_to_pwr_good_ms": 100,
            "compound": False,
            "ports": ports,
        }

    @staticmethod
    def _json(status: int, body: Any) -> httpx.Response:
        return httpx.Response(status, json=body)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        if self.fail_status is not None:
            return self._json(self.fail_status, {"ok": False, "error": "forced failure"})
        if request.method != "GET" and self.token:
            if request.headers.get("Authorization") != f"Bearer {self.token}":
                return self._json(401, {"error": "unauthorized"})
        if path == "/ping":
            return self._json(200, {"ok": True})
        if path == "/api/usb/devices":
            return self._json(200, {"devices": list(self.devices.values())})
        if path.startswith("/api/usb/devices/"):
            busid = path.rsplit("/", 1)[1]
            if busid not in self.devices:
                return self._json(404, {"ok": False, "error": "no such device"})
            return self._json(200, self.devices[busid])
        if path == "/api/usb/hubs":
            return self._json(200, {"hubs": [self._hub()]})
        if path.startswith("/api/ports/") and request.method == "POST":
            _, _, _, port, action = path.split("/")
            if port not in self.power:
                return self._json(404, {"ok": False, "error": "unknown port"})
            force = bool((body or {}).get("force"))
            if self.switching != "per-port" and not force:
                return self._json(
                    409, {"ok": False, "error": f"hub 1-1 is {self.switching} switched"}
                )
            if action in ("on", "off"):
                self._set_power(port, action == "on")
                return self._json(200, {"ok": True, "port": port, "action": action})
            if action == "cycle":
                return self._json(
                    200,
                    {
                        "ok": True,
                        "port": port,
                        "action": "cycle",
                        "off_ms": (body or {}).get("off_ms", 1000),
                        "async": True,
                    },
                )
        if path.startswith("/api/"):  # analog mux surface
            return self._json(200, {"ok": True, "active": None})
        return self._json(404, {"error": "Not Found"})

    def _set_power(self, port: str, on: bool) -> None:
        self.power[port] = on
        if on and port in self.parked:
            self.devices[port] = self.parked.pop(port)
        elif not on and port in self.devices:
            self.parked[port] = self.devices.pop(port)

    def posts(self) -> list[tuple[str, Any]]:
        return [(p, b) for (m, p, b) in self.calls if m == "POST"]
