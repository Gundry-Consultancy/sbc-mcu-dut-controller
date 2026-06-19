"""Tests for /ui/usbip bench-wide overview (M3.5+).

Three endpoints:
  GET  /ui/usbip                  - page with one placeholder per host
  GET  /ui/usbip/host/{host_id}   - HTMX fragment listing one host's busids
  POST /ui/usbip/assign           - assigns busid to a device, returns fragment

Tests use a stub host_registry whose transport_for returns an AsyncMock
that replays canned `usbip list -l` output.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from hil_controller.db.connection import get_db
from hil_controller.hosts.base import ExecResult

TOKEN = "test-token-for-ci"
COOKIE = {"hil_token": TOKEN}

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
    |__ Port 2: Dev 15, If 0, Class=Vendor Specific Class, Driver=usbfs, 480M
"""

UHUBCTL_OUTPUT = """\
Current status for hub 1-1.1.1 [2109:0817 VIA Labs, Inc. USB2.0 Hub, USB 2.10, 4 ports, ppps]
  Port 1: 0100 power
  Port 3: 0503 power highspeed enable connect [239a:8143]
  Port 4: 0503 power highspeed enable connect [239a:8123]
"""


DEV_LINKS_OUTPUT = (
    "/dev/serial/by-id\tusb-Adafruit_QT_Py_ESP32-S3__4MB_Flash_2MB_PS-if00\t/dev/ttyACM0\n"
    "/dev/disk/by-label\tWIPPER\t/dev/sdb\n"
    "/dev/disk/by-path\tplatform-xhci-hcd.0-usb-0:1.2:1.0-scsi-0:0:0:0\t/dev/sdb\n"
)


def _result(exit_status: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    r = MagicMock(spec=ExecResult)
    r.exit_status = exit_status
    r.stdout = stdout
    r.stderr = stderr
    return r


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
async def app_with_usbip(tmp_path: Path):
    import os

    db_file = str(tmp_path / "usbip.db")
    os.environ["HIL_DB_PATH"] = db_file

    from hil_controller.main import create_app

    application = create_app(db_path=db_file)
    async with application.router.lifespan_context(application):
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
                    "[]",
                    "available",
                ),
            )
            # Device 1: already has busid 1-1.1.1.4 -> appears "matched"
            await db.execute(
                """INSERT INTO devices (id, host_id, hub_host_id, hub_port_path,
                       kind, model, capabilities_json, status, pool)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "mcu-revtft",
                    "rpi-displays",
                    "rpi-displays",
                    "1-1.1.1.4",
                    "microcontroller",
                    "feather_esp32s3_reverse_tft",
                    "[]",
                    "available",
                    "public",
                ),
            )
            # Device 2: same hub but no busid yet -> shows up in assign dropdown
            await db.execute(
                """INSERT INTO devices (id, host_id, hub_host_id, hub_port_path,
                       kind, model, capabilities_json, status, pool)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "mcu-qtpy",
                    "rpi-displays",
                    "rpi-displays",
                    None,
                    "microcontroller",
                    "qtpy_esp32s3",
                    "[]",
                    "available",
                    "public",
                ),
            )
            # Device 3: on a different host - should NOT appear in dropdown
            await db.execute(
                """INSERT INTO hosts (id, role, addr, transport, ssh_user, ssh_key_path,
                       max_concurrent_jobs, capabilities_json, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "other-host",
                    "microcontroller-fleet",
                    "10.0.0.1",
                    "ssh",
                    "pi",
                    "/tmp/k",
                    None,
                    "[]",
                    "available",
                ),
            )
            await db.execute(
                """INSERT INTO devices (id, host_id, hub_host_id, hub_port_path,
                       kind, model, capabilities_json, status, pool)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "mcu-on-other",
                    "other-host",
                    "other-host",
                    None,
                    "microcontroller",
                    "x",
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
async def usbip_client(app_with_usbip):
    async with AsyncClient(
        transport=ASGITransport(app=app_with_usbip),
        base_url="http://test",
    ) as ac:
        yield ac, app_with_usbip


# --------------------------------------------------------------------------- #
# Auth guard                                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_overview_redirects_without_cookie(usbip_client) -> None:
    client, _ = usbip_client
    r = await client.get("/ui/usbip", follow_redirects=False)
    assert r.status_code == 303
    assert "/ui/login" in r.headers["location"]


@pytest.mark.asyncio
async def test_fragment_redirects_without_cookie(usbip_client) -> None:
    client, _ = usbip_client
    r = await client.get("/ui/usbip/host/rpi-displays", follow_redirects=False)
    assert r.status_code == 303


# --------------------------------------------------------------------------- #
# Overview page                                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_overview_lists_hosts_with_placeholders(usbip_client) -> None:
    client, _ = usbip_client
    r = await client.get("/ui/usbip", cookies=COOKIE)
    assert r.status_code == 200
    body = r.text
    # nav active state
    assert 'href="/ui/usbip"' in body
    # one section per host with hx-get on the placeholder
    assert "rpi-displays" in body
    assert "other-host" in body
    assert 'hx-get="/ui/usbip/host/rpi-displays"' in body
    assert 'hx-get="/ui/usbip/host/other-host"' in body


@pytest.mark.asyncio
async def test_overview_empty_when_no_hosts(client) -> None:
    # Uses the base `client` fixture (no host_registry, no hosts seeded).
    r = await client.get("/ui/usbip", cookies=COOKIE)
    assert r.status_code == 200
    assert "No hosts in topology" in r.text


# --------------------------------------------------------------------------- #
# Per-host fragment                                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fragment_renders_busid_rows_with_matched_device(usbip_client) -> None:
    client, _ = usbip_client
    r = await client.get("/ui/usbip/host/rpi-displays", cookies=COOKIE)
    assert r.status_code == 200
    body = r.text
    # The pre-assigned busid points at its device
    assert "1-1.1.1.4" in body
    assert "mcu-revtft" in body
    # Unmatched busids show the assign dropdown with the OTHER device on this hub
    assert "mcu-qtpy" in body
    # Other-host device should NOT appear (not on this hub)
    assert "mcu-on-other" not in body
    assert "Feather ESP32-S3 Reverse TFT" in body
    assert "E66141040383622E" in body
    assert "cdc_acm" in body
    assert "Hub power status" in body
    assert "Port 4" in body
    assert "power highspeed enable connect" in body


@pytest.mark.asyncio
async def test_fragment_renders_daemon_down_alert_on_nonzero_exit(usbip_client) -> None:
    client, app = usbip_client

    async def daemon_down(argv, **_kwargs):
        if argv == ["sudo", "-n", "/usr/sbin/usbip", "list", "-l"]:
            return _result(1, stderr="usbipd not running")
        return await _default_exec(argv, **_kwargs)

    app.state.stub_transport.exec = AsyncMock(side_effect=daemon_down)
    r = await client.get("/ui/usbip/host/rpi-displays", cookies=COOKIE)
    assert r.status_code == 200
    assert "usbipd not reachable" in r.text
    assert "usbipd not running" in r.text


@pytest.mark.asyncio
async def test_fragment_handles_unknown_host_gracefully(usbip_client) -> None:
    client, app = usbip_client
    app.state.host_registry.transport_for.side_effect = KeyError("nope")
    r = await client.get("/ui/usbip/host/no-such", cookies=COOKIE)
    assert r.status_code == 200
    assert "unknown host" in r.text


@pytest.mark.asyncio
async def test_fragment_handles_missing_registry(usbip_client) -> None:
    client, app = usbip_client
    app.state.host_registry = None
    r = await client.get("/ui/usbip/host/rpi-displays", cookies=COOKIE)
    assert r.status_code == 200
    assert "host registry not loaded" in r.text


# --------------------------------------------------------------------------- #
# Serial & flashing /dev paths (arduino-tagged hosts only)                    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fragment_shows_dev_links_for_arduino_host(usbip_client) -> None:
    client, app = usbip_client
    async with get_db(app.state.db_path) as db:
        await db.execute(
            "UPDATE devices SET capabilities_json = ? WHERE id = ?",
            ('["arduino", "wippersnapper"]', "mcu-revtft"),
        )
        await db.commit()
    app.state.stub_transport.exec = AsyncMock(side_effect=_exec_with_dev_links)

    r = await client.get("/ui/usbip/host/rpi-displays", cookies=COOKIE)
    assert r.status_code == 200
    body = r.text
    assert "Serial &amp; flashing paths" in body
    assert "usb-Adafruit_QT_Py_ESP32-S3__4MB_Flash_2MB_PS-if00" in body
    assert "WIPPER" in body
    # by-path is the last-resort group: rendered in its own collapsed
    # <details>, split into indented bus-type segments (not one long string).
    assert "full USB/PCI topology" in body
    assert "platform-xhci-hcd.0" in body
    assert "usb-0:1.2:1.0" in body
    assert "scsi-0:0:0:0" in body
    assert "platform-xhci-hcd.0-usb-0:1.2:1.0-scsi-0:0:0:0" not in body
    assert "padding-left:" in body  # progressive indentation


@pytest.mark.asyncio
async def test_fragment_skips_dev_links_for_non_arduino_host(usbip_client) -> None:
    from hil_controller.adapters.usbip_inventory import _DEV_LINKS_CMD

    client, app = usbip_client
    # default seeded devices have capabilities "[]" — not arduino
    r = await client.get("/ui/usbip/host/rpi-displays", cookies=COOKIE)
    assert r.status_code == 200
    assert "Serial &amp; flashing paths" not in r.text
    argv_list = [c.args[0] for c in app.state.stub_transport.exec.call_args_list]
    assert _DEV_LINKS_CMD not in argv_list


# --------------------------------------------------------------------------- #
# Assign endpoint                                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_assign_writes_hub_host_and_busid_to_device_record(
    usbip_client,
) -> None:
    client, app = usbip_client
    db_path = app.state.db_path

    r = await client.post(
        "/ui/usbip/assign",
        data={"host_id": "rpi-displays", "busid": "1-1.1.1.3", "device_id": "mcu-qtpy"},
        cookies=COOKIE,
    )
    assert r.status_code == 200
    # The response is the re-rendered fragment, so it should show the new
    # assignment in place.
    assert "1-1.1.1.3" in r.text
    assert "mcu-qtpy" in r.text

    async with get_db(db_path) as db:
        async with db.execute(
            "SELECT hub_host_id, hub_port_path FROM devices WHERE id = ?",
            ("mcu-qtpy",),
        ) as cur:
            row = await cur.fetchone()
    assert row["hub_host_id"] == "rpi-displays"
    assert row["hub_port_path"] == "1-1.1.1.3"


@pytest.mark.asyncio
async def test_assign_returns_error_for_unknown_device(usbip_client) -> None:
    client, _ = usbip_client
    r = await client.post(
        "/ui/usbip/assign",
        data={"host_id": "rpi-displays", "busid": "1-1.1.1.3", "device_id": "nope"},
        cookies=COOKIE,
    )
    assert r.status_code == 200
    assert "device not found" in r.text


@pytest.mark.asyncio
async def test_assign_redirects_without_auth(usbip_client) -> None:
    client, _ = usbip_client
    r = await client.post(
        "/ui/usbip/assign",
        data={"host_id": "rpi-displays", "busid": "1-1.1.1.3", "device_id": "mcu-qtpy"},
        follow_redirects=False,
    )
    assert r.status_code == 303


@pytest.mark.asyncio
async def test_assign_is_idempotent_on_already_assigned_busid(
    usbip_client,
) -> None:
    client, app = usbip_client
    db_path = app.state.db_path
    # mcu-revtft already has 1-1.1.1.4 — re-assigning the same value is a no-op.
    r = await client.post(
        "/ui/usbip/assign",
        data={"host_id": "rpi-displays", "busid": "1-1.1.1.4", "device_id": "mcu-revtft"},
        cookies=COOKIE,
    )
    assert r.status_code == 200
    async with get_db(db_path) as db:
        async with db.execute(
            "SELECT hub_port_path FROM devices WHERE id = ?", ("mcu-revtft",)
        ) as cur:
            row = await cur.fetchone()
    assert row["hub_port_path"] == "1-1.1.1.4"
