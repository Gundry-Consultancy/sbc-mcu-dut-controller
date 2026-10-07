"""EspUsbipBridgeClient + BridgePortPower against a fake esp-usbip-bridge HTTP API."""

from __future__ import annotations

import httpx
import pytest

from hil_controller.adapters import esp_usbip_bridge as eub
from hil_controller.adapters.esp_usbip_bridge import (
    BridgePortPower,
    EspBridgeAuthError,
    EspBridgeBusyError,
    EspBridgeError,
    EspBridgeNotFoundError,
    EspBridgeNotSwitchableError,
    EspUsbipBridgeClient,
    bridge_base_url,
    bridge_power_force,
    find_hub_port,
    uses_bridge_port_power,
)
from tests.fake_esp_bridge import BASE_URL, FakeBridge


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Presence polling + off_s waits run instantly."""

    async def _instant(_s):
        return None

    monkeypatch.setattr(eub.asyncio, "sleep", _instant)


# --------------------------------------------------------------------------- #
# helpers                                                                     #
# --------------------------------------------------------------------------- #


def test_bridge_base_url_defaults_to_http_addr_port_80():
    assert bridge_base_url("usbip-a1b2c3.local") == "http://usbip-a1b2c3.local"
    assert bridge_base_url("10.0.0.5", "http://10.0.0.5:8080/") == "http://10.0.0.5:8080"
    with pytest.raises(ValueError):
        bridge_base_url("", None)


def test_power_control_parsing():
    assert bridge_power_force({"power_control": "bridge-port"}) is False
    assert bridge_power_force({"power_control": "Bridge-Port:force"}) is True
    assert bridge_power_force({"power_control": None}) is None
    assert bridge_power_force({"solenoid_channel": 3}) is None
    assert uses_bridge_port_power({"power_control": "bridge-port", "hub_port_path": "1-1.1"})
    assert not uses_bridge_port_power({"power_control": "bridge-port"})  # no port path


def test_find_hub_port():
    hubs = [{"path": "1-1", "ports": [{"port": 1, "path": "1-1.1"}, {"port": 2, "path": "1-1.2"}]}]
    hub, port = find_hub_port(hubs, "1-1.2")
    assert hub["path"] == "1-1" and port["port"] == 2
    assert find_hub_port(hubs, "1-1.9") is None


# --------------------------------------------------------------------------- #
# client                                                                      #
# --------------------------------------------------------------------------- #


async def test_ping_and_devices():
    fake = FakeBridge()
    client = fake.client()
    assert await client.ping() is True
    devices = await client.devices()
    assert [d["busid"] for d in devices] == ["1-1.1"]
    assert fake.calls[0][:2] == ("GET", "/ping")


async def test_ping_false_when_unreachable():
    def boom(request):
        raise httpx.ConnectError("no route to host")

    client = EspUsbipBridgeClient(
        BASE_URL, client=httpx.AsyncClient(transport=httpx.MockTransport(boom))
    )
    assert await client.ping() is False
    with pytest.raises(EspBridgeError):
        await client.devices()


async def test_device_presence_200_and_404():
    fake = FakeBridge()
    client = fake.client()
    assert await client.device_present("1-1.1") is True
    assert await client.device("1-1.2") is None  # 404 → absent, not an error
    assert await client.device_present("1-1.2") is False


async def test_port_power_posts_force_body_and_token():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = request.content
        return httpx.Response(200, json={"ok": True, "port": "1-1.3", "action": "off"})

    client = EspUsbipBridgeClient(
        BASE_URL, token="sekret", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    result = await client.port_off("1-1.3", force=True)
    assert seen["url"] == f"{BASE_URL}/api/ports/1-1.3/off"
    assert seen["auth"] == "Bearer sekret"
    assert b'"force":true' in seen["body"].replace(b" ", b"")
    assert result["action"] == "off"


async def test_port_cycle_sends_off_ms():
    fake = FakeBridge()
    result = await fake.client().port_cycle("1-1.1", off_ms=1500)
    assert result["async"] is True
    assert fake.posts() == [("/api/ports/1-1.1/cycle", {"off_ms": 1500, "force": False})]


@pytest.mark.parametrize(
    ("status", "exc"),
    [
        (401, EspBridgeAuthError),
        (404, EspBridgeNotFoundError),
        (409, EspBridgeNotSwitchableError),
        (503, EspBridgeBusyError),
        (400, EspBridgeError),
    ],
)
async def test_error_statuses_map_to_exceptions(status, exc):
    fake = FakeBridge()
    fake.fail_status = status
    with pytest.raises(exc) as info:
        await fake.client().port_on("1-1.1")
    assert info.value.status == status
    assert info.value.error == "forced failure"


async def test_missing_token_is_401():
    fake = FakeBridge(token="sekret")
    client = EspUsbipBridgeClient(
        BASE_URL, client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handle))
    )
    assert await client.devices()  # GETs need no token
    with pytest.raises(EspBridgeAuthError):
        await client.port_off("1-1.1")


# --------------------------------------------------------------------------- #
# BridgePortPower                                                             #
# --------------------------------------------------------------------------- #


async def test_power_cycle_awaits_disappear_then_reappear():
    fake = FakeBridge()
    lines: list[str] = []
    power = BridgePortPower(fake.client(), "1-1.1")
    assert await power.power_cycle(off_s=1.0, settle_s=2.0, on_line=lines.append) is True
    assert [p for p, _ in fake.posts()] == ["/api/ports/1-1.1/off", "/api/ports/1-1.1/on"]
    # presence was polled via GET /api/usb/devices/{busid}, not exec
    presence = [p for (m, p, _) in fake.calls if m == "GET" and p == "/api/usb/devices/1-1.1"]
    assert len(presence) >= 3  # pre-check, gone, back
    assert any("disappeared" in ln for ln in lines)
    assert any("re-enumerated" in ln for ln in lines)
    assert "1-1.1" in fake.devices


async def test_power_cycle_reports_device_not_returning():
    fake = FakeBridge()
    power = BridgePortPower(fake.client(), "1-1.1")
    # The device is unplugged while off: switching on brings nothing back.
    original = fake._set_power

    def _set_power(port, on):
        original(port, on)
        if on:
            fake.devices.pop(port, None)

    fake._set_power = _set_power
    lines: list[str] = []
    assert await power.power_cycle(reappear_timeout_s=0.0, on_line=lines.append) is False
    assert any("did not re-enumerate" in ln for ln in lines)


async def test_ganged_hub_409_is_explained_and_force_opt_in_works():
    fake = FakeBridge(switching="ganged")
    power = BridgePortPower(fake.client(), "1-1.1")
    with pytest.raises(EspBridgeNotSwitchableError) as info:
        await power.off()
    assert "bridge-port:force" in str(info.value)
    assert info.value.status == 409
    forced = BridgePortPower(fake.client(), "1-1.1", force=True)
    await forced.off()
    assert fake.power["1-1.1"] is False
    assert fake.posts()[-1] == ("/api/ports/1-1.1/off", {"force": True})


async def test_presence_swallows_errors_as_absent():
    fake = FakeBridge()
    fake.fail_status = 503
    assert await BridgePortPower(fake.client(), "1-1.1").present() is False


async def test_await_presence_times_out():
    fake = FakeBridge()
    power = BridgePortPower(fake.client(), "1-1.2")
    assert await power.await_presence(True, timeout_s=0.0) is False
    assert await power.await_presence(False, timeout_s=0.0) is True
