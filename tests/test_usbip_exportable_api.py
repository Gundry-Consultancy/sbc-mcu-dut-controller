"""Tests for GET /v1/hosts/{id}/usbip/exportable (M3.5)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from hil_controller.hosts.base import ExecResult


def _result(exit_status: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    r = MagicMock(spec=ExecResult)
    r.exit_status = exit_status
    r.stdout = stdout
    r.stderr = stderr
    return r


USBIP_LIST_OUTPUT = """\
 - busid 1-1.1.1.4 (239a:8123)
   Adafruit Industries : Feather ESP32-S3 Reverse TFT (239a:8123)

 - busid 1-1.1.1.3 (239a:8143)
   Adafruit Industries : QT Py ESP32-S3 (239a:8143)

 - busid 1-1.2 (239a:8053)
   Adafruit Industries : PyPortal M4 Titano (239a:8053)
"""

LSUSB_OUTPUT = """\
Bus 001 Device 014: ID 239a:8123 Adafruit Feather ESP32-S3 Reverse TFT
Bus 001 Device 013: ID 239a:8143 Adafruit QT Py ESP32-S3
Bus 001 Device 015: ID 239a:8053 Adafruit PyPortal M4 Titano
"""

LSUSB_VERBOSE_OUTPUT = """\
Bus 001 Device 014: ID 239a:8123 Adafruit Feather ESP32-S3 Reverse TFT
Device Descriptor:
  bDeviceClass            0 (Defined at Interface level)
  bNumInterfaces          2
  iManufacturer           1 Adafruit
  iProduct                2 Feather ESP32-S3 Reverse TFT
  iSerial                 3 E66141040383622E
  MaxPower              500mA

Bus 001 Device 013: ID 239a:8143 Adafruit QT Py ESP32-S3
Device Descriptor:
  bDeviceClass          255 Vendor Specific Class
  bNumInterfaces          1
  iManufacturer           1 Adafruit
  iProduct                2 QT Py ESP32-S3
  iSerial                 3 7C9E1234
  MaxPower              100mA
"""

LSUSB_TREE_OUTPUT = """\
/:  Bus 01.Port 1: Dev 1, Class=root_hub, Driver=xhci_hcd/1p, 480M
    |__ Port 1: Dev 2, If 0, Class=Hub, Driver=hub/4p, 480M
        |__ Port 1: Dev 3, If 0, Class=Hub, Driver=hub/4p, 480M
            |__ Port 1: Dev 4, If 0, Class=Hub, Driver=hub/4p, 480M
                |__ Port 3: Dev 13, If 0, Class=Vendor Specific Class, Driver=cdc_acm, 12M
                |__ Port 4: Dev 14, If 0, Class=Vendor Specific Class, Driver=cdc_acm, 12M
"""

UHUBCTL_OUTPUT = """\
Current status for hub 1-1.1.1 [2109:0817 VIA Labs, Inc. USB2.0 Hub, USB 2.10, 4 ports, ppps]
  Port 4: 0503 power highspeed enable connect [239a:8123]
"""


DEV_LINKS_OUTPUT = (
    "/dev/serial/by-id\tusb-Adafruit_QT_Py_ESP32-S3__4MB_Flash_2MB_PS-if00\t/dev/ttyACM0\n"
    "/dev/disk/by-label\tWIPPER\t/dev/sdb\n"
)


async def _default_exec(argv, **_kwargs):
    if argv == ["sudo", "-n", "/usr/sbin/usbip", "list", "-l"]:
        return _result(0, stdout=USBIP_LIST_OUTPUT)
    if argv == ["sh", "-lc", "lsusb 2>/dev/null || true"]:
        return _result(0, stdout=LSUSB_OUTPUT)
    if argv == ["sh", "-lc", "sudo -n lsusb -v 2>/dev/null || lsusb -v 2>/dev/null || true"]:
        return _result(0, stdout=LSUSB_VERBOSE_OUTPUT)
    if argv == ["sh", "-lc", "lsusb -t 2>/dev/null || true"]:
        return _result(0, stdout=LSUSB_TREE_OUTPUT)
    if argv == ["sh", "-lc", "sudo -n uhubctl 2>/dev/null || uhubctl 2>/dev/null || true"]:
        return _result(0, stdout=UHUBCTL_OUTPUT)
    raise AssertionError(f"unexpected argv: {argv}")


async def _exec_with_dev_links(argv, **kwargs):
    from hil_controller.adapters.usbip_inventory import _DEV_LINKS_CMD

    if argv == _DEV_LINKS_CMD:
        return _result(0, stdout=DEV_LINKS_OUTPUT)
    return await _default_exec(argv, **kwargs)


@pytest_asyncio.fixture
async def app_with_registry(tmp_path: Path):
    """Spin up an app with a stub host_registry whose transport can be controlled."""
    import os

    db_file = str(tmp_path / "test.db")
    os.environ["HIL_DB_PATH"] = db_file

    from hil_controller.main import create_app

    application = create_app(db_path=db_file)
    async with application.router.lifespan_context(application):
        from hil_controller.db.connection import get_db

        async with get_db(db_file) as db:
            await db.execute(
                """INSERT INTO hosts (id, role, addr, transport, ssh_user, ssh_key_path,
                       max_concurrent_jobs, capabilities_json, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "rpi-displays",
                    "microcontroller-fleet",
                    "192.168.1.234",
                    "ssh",
                    "pi",
                    "/etc/hil/keys/rpi-displays",
                    None,
                    '["microcontrollers"]',
                    "available",
                ),
            )
            await db.execute(
                """INSERT INTO devices (id, host_id, hub_host_id, hub_port_path, kind, model,
                       capabilities_json, status, pool)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "mcu-feather-esp32s3-revtft",
                    "rpi-displays",
                    "rpi-displays",
                    "1-1.1.1.4",
                    "microcontroller",
                    "feather-esp32s3-revtft",
                    "[]",
                    "available",
                    "public",
                ),
            )
            await db.commit()

        registry = MagicMock()
        transport = AsyncMock()
        transport.exec = AsyncMock(side_effect=_default_exec)
        registry.transport_for = MagicMock(return_value=transport)
        application.state.host_registry = registry
        application.state.stub_transport = transport

        yield application


@pytest_asyncio.fixture
async def authed_client_with_registry(app_with_registry):
    async with AsyncClient(
        transport=ASGITransport(app=app_with_registry),
        base_url="http://test",
        headers={"Authorization": "Bearer test-token-for-ci"},
    ) as ac:
        yield ac, app_with_registry


@pytest.mark.asyncio
async def test_exportable_returns_parsed_busids_with_matched_device(
    authed_client_with_registry,
) -> None:
    client, _ = authed_client_with_registry

    resp = await client.get("/v1/hosts/rpi-displays/usbip/exportable")
    assert resp.status_code == 200
    body = resp.json()
    assert body["host_id"] == "rpi-displays"
    assert body["daemon_listening"] is True
    busids = body["busids"]
    assert len(busids) == 3

    revtft = next(b for b in busids if b["busid"] == "1-1.1.1.4")
    assert revtft["vid"] == "239a"
    assert revtft["pid"] == "8123"
    assert revtft["matched_device_id"] == "mcu-feather-esp32s3-revtft"
    assert revtft["product"] == "Feather ESP32-S3 Reverse TFT"
    assert revtft["manufacturer"] == "Adafruit"
    assert revtft["serial"] == "E66141040383622E"
    assert revtft["speed"] == "12M"
    assert revtft["driver"] == "cdc_acm"
    assert revtft["port_power_status"] == "on"
    assert revtft["port_connect_status"] == "connected"

    qtpy = next(b for b in busids if b["busid"] == "1-1.1.1.3")
    assert qtpy["matched_device_id"] is None


@pytest.mark.asyncio
async def test_exportable_calls_usbip_list_with_sudo_n(
    authed_client_with_registry,
) -> None:
    client, app = authed_client_with_registry

    await client.get("/v1/hosts/rpi-displays/usbip/exportable")
    argv_list = [call.args[0] for call in app.state.stub_transport.exec.call_args_list]
    assert ["sudo", "-n", "/usr/sbin/usbip", "list", "-l"] in argv_list


@pytest.mark.asyncio
async def test_exportable_reports_daemon_not_listening_on_nonzero_exit(
    authed_client_with_registry,
) -> None:
    client, app = authed_client_with_registry

    async def daemon_down(argv, **_kwargs):
        if argv == ["sudo", "-n", "/usr/sbin/usbip", "list", "-l"]:
            return _result(1, stderr="usbip: error: failed to open libusbip: usbipd not running")
        return await _default_exec(argv, **_kwargs)

    app.state.stub_transport.exec = AsyncMock(side_effect=daemon_down)

    resp = await client.get("/v1/hosts/rpi-displays/usbip/exportable")
    assert resp.status_code == 200
    body = resp.json()
    assert body["daemon_listening"] is False
    assert body["busids"] == []
    assert "usbipd not running" in body["error"]


@pytest.mark.asyncio
async def test_exportable_404_when_host_unknown(
    authed_client_with_registry,
) -> None:
    client, _ = authed_client_with_registry
    resp = await client.get("/v1/hosts/no-such-host/usbip/exportable")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_exportable_503_when_registry_missing(
    authed_client_with_registry,
) -> None:
    client, app = authed_client_with_registry
    app.state.host_registry = None
    resp = await client.get("/v1/hosts/rpi-displays/usbip/exportable")
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_exportable_includes_dev_links_for_arduino_host(
    authed_client_with_registry,
) -> None:
    client, app = authed_client_with_registry
    from hil_controller.db.connection import get_db

    async with get_db(app.state.db_path) as db:
        await db.execute(
            "UPDATE devices SET capabilities_json = ? WHERE id = ?",
            ('["arduino", "wippersnapper"]', "mcu-feather-esp32s3-revtft"),
        )
        await db.commit()
    app.state.stub_transport.exec = AsyncMock(side_effect=_exec_with_dev_links)

    resp = await client.get("/v1/hosts/rpi-displays/usbip/exportable")
    assert resp.status_code == 200
    dev_links = resp.json()["dev_links"]
    assert dev_links is not None
    assert dev_links["serial_by_id"][0]["name"] == (
        "usb-Adafruit_QT_Py_ESP32-S3__4MB_Flash_2MB_PS-if00"
    )
    assert dev_links["serial_by_id"][0]["target"] == "/dev/ttyACM0"
    assert any(e["name"] == "WIPPER" for e in dev_links["disk_by_label"])


@pytest.mark.asyncio
async def test_exportable_omits_dev_links_for_non_arduino_host(
    authed_client_with_registry,
) -> None:
    from hil_controller.adapters.usbip_inventory import _DEV_LINKS_CMD

    client, app = authed_client_with_registry
    resp = await client.get("/v1/hosts/rpi-displays/usbip/exportable")
    assert resp.status_code == 200
    assert resp.json()["dev_links"] is None
    argv_list = [c.args[0] for c in app.state.stub_transport.exec.call_args_list]
    assert _DEV_LINKS_CMD not in argv_list


@pytest.mark.asyncio
async def test_exportable_requires_auth(app_with_registry) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app_with_registry),
        base_url="http://test",
    ) as ac:
        resp = await ac.get("/v1/hosts/rpi-displays/usbip/exportable")
    assert resp.status_code in (401, 422, 403)
