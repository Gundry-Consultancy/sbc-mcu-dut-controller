"""Tests for usb_scan: parse `usbip list -l` + passive learn."""

from __future__ import annotations

import os
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

_TOPOLOGY = """
hosts:
  - id: hub-a
    role: microcontroller-fleet
    addr: 127.0.0.10
    transport: fake
    ssh_user: pi
    ssh_key_path: /tmp/k
    capabilities: [usbip-server]

devices:
  - id: pyportal
    host_id: hub-a
    kind: microcontroller
    pool: public
    status: available
    hub_port_path: "1-1.1.3"
    usb_ids:
      - { vid: "239a", pid: "8053", role: runtime }
"""

LSUSB_OUTPUT = """\
Bus 001 Device 014: ID 239a:8123 Adafruit Feather ESP32-S3 Reverse TFT
Bus 001 Device 013: ID 239a:8143 Adafruit QT Py ESP32-S3
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
  Port 1: 0100 power
  Port 3: 0503 power highspeed enable connect [239a:8143]
  Port 4: 0503 power highspeed enable connect [239a:8123]
"""


@pytest_asyncio.fixture
async def app(tmp_path: Path):
    db_file = str(tmp_path / "scan.db")
    topo = tmp_path / "t.yaml"
    topo.write_text(_TOPOLOGY)
    os.environ["HIL_DB_PATH"] = db_file
    from hil_controller.main import create_app

    a = create_app(db_path=db_file, topology_file=str(topo))
    async with a.router.lifespan_context(a):
        a.state._test_db = db_file
        yield a


# -- parse_usbip_list ----------------------------------------------------


def test_parse_usbip_list_basic():
    from hil_controller.adapters.usb_scan import parse_usbip_list

    text = """\
 - busid 1-1.1.3 (239a:8053)
   Adafruit Industries LLC : unknown product (239a:8053)

 - busid 1-1.1.4 (239a:80df)
   Adafruit Industries LLC : QT Py ESP32-S2 (239a:80df)
"""
    rows = parse_usbip_list(text)
    assert len(rows) == 2
    assert rows[0]["busid"] == "1-1.1.3"
    assert rows[0]["vid"] == "239a"
    assert rows[0]["pid"] == "8053"
    assert rows[1]["busid"] == "1-1.1.4"
    assert rows[1]["pid"] == "80df"
    # Description captured
    assert "QT Py" in rows[1]["description"]


def test_parse_usbip_list_empty():
    from hil_controller.adapters.usb_scan import parse_usbip_list

    assert parse_usbip_list("") == []
    assert parse_usbip_list("usbip: no exportable devices found") == []


def test_parse_usbip_list_normalises_case():
    from hil_controller.adapters.usb_scan import parse_usbip_list

    text = " - busid 1-2 (239A:8053)\n   Vendor : Product (239A:8053)\n"
    rows = parse_usbip_list(text)
    assert rows[0]["vid"] == "239a"
    assert rows[0]["pid"] == "8053"


def test_parse_lsusb():
    from hil_controller.adapters.usb_scan import parse_lsusb

    rows = parse_lsusb(LSUSB_OUTPUT)
    assert rows == [
        {
            "bus": 1,
            "device": 14,
            "vid": "239a",
            "pid": "8123",
            "description": "Adafruit Feather ESP32-S3 Reverse TFT",
        },
        {
            "bus": 1,
            "device": 13,
            "vid": "239a",
            "pid": "8143",
            "description": "Adafruit QT Py ESP32-S3",
        },
    ]


def test_parse_lsusb_verbose():
    from hil_controller.adapters.usb_scan import parse_lsusb_verbose

    rows = parse_lsusb_verbose(LSUSB_VERBOSE_OUTPUT)
    first = rows[0]
    assert first["bus"] == 1
    assert first["device"] == 14
    assert first["manufacturer"] == "Adafruit"
    assert first["product"] == "Feather ESP32-S3 Reverse TFT"
    assert first["serial"] == "E66141040383622E"
    assert first["num_interfaces"] == 2
    assert first["max_power"] == "500mA"
    assert first["device_class"] == "0 (Defined at Interface level)"


def test_parse_lsusb_tree():
    from hil_controller.adapters.usb_scan import parse_lsusb_tree

    rows = parse_lsusb_tree(LSUSB_TREE_OUTPUT)
    qtpy = next(row for row in rows if row["device"] == 13)
    revtft = next(row for row in rows if row["device"] == 14)
    assert qtpy["busid"] == "1-1.1.1.3"
    assert qtpy["driver"] == "cdc_acm"
    assert qtpy["speed"] == "12M"
    assert revtft["busid"] == "1-1.1.1.4"


def test_parse_uhubctl():
    from hil_controller.adapters.usb_scan import parse_uhubctl

    rows = parse_uhubctl(UHUBCTL_OUTPUT)
    assert len(rows) == 1
    hub = rows[0]
    assert hub["location"] == "1-1.1.1"
    assert hub["hub_vid_pid"] == "2109:0817"
    assert "VIA Labs" in hub["hub_description"]
    port4 = next(port for port in hub["ports"] if port["port_number"] == 4)
    assert port4["power_status"] == "on"
    assert port4["connect_status"] == "connected"
    assert "239a:8123" in port4["status"]


DEV_LINKS_OUTPUT = (
    "/dev/serial/by-id\tusb-Adafruit_QT_Py_ESP32-S3__4MB_Flash_2MB_PS-if00\t/dev/ttyACM0\n"
    "/dev/serial/by-path\tplatform-3f980000.usb-usb-0:1.2:1.0\t/dev/ttyACM0\n"
    "/dev/disk/by-id\tusb-Adafruit_External_Flash-0:0\t/dev/sdb\n"
    "/dev/disk/by-id\tmmc-SE32G_0x7a5a1c33\t/dev/mmcblk0\n"
    "/dev/disk/by-label\tWIPPER\t/dev/sdb\n"
    "/dev/disk/by-label\tbootfs\t/dev/mmcblk0p1\n"
    "/dev/disk/by-path\tplatform-fe340000.spi-cs-0\t/dev/mmcblk0\n"
)


def test_parse_dev_links_groups_by_category():
    from hil_controller.adapters.usb_scan import parse_dev_links

    grouped = parse_dev_links(DEV_LINKS_OUTPUT)
    assert grouped["serial_by_id"] == [
        {
            "name": "usb-Adafruit_QT_Py_ESP32-S3__4MB_Flash_2MB_PS-if00",
            "target": "/dev/ttyACM0",
        }
    ]
    assert grouped["serial_by_path"][0]["target"] == "/dev/ttyACM0"
    # disk_by_id is sorted by name (mmc- before usb-)
    assert [e["name"] for e in grouped["disk_by_id"]] == [
        "mmc-SE32G_0x7a5a1c33",
        "usb-Adafruit_External_Flash-0:0",
    ]
    labels = {e["name"]: e["target"] for e in grouped["disk_by_label"]}
    assert labels["WIPPER"] == "/dev/sdb"
    assert grouped["disk_by_path"] == [
        {"name": "platform-fe340000.spi-cs-0", "target": "/dev/mmcblk0"}
    ]


def test_split_dev_path_segments_by_bus_type():
    from hil_controller.adapters.usb_scan import split_dev_path

    assert split_dev_path("platform-3f980000.usb-usb-0:1.2:1.0-scsi-0:0:0:0") == [
        "platform-3f980000.usb",
        "usb-0:1.2:1.0",
        "scsi-0:0:0:0",
    ]
    assert split_dev_path("pci-0000:00:14.0-usb-0:3:1.0") == [
        "pci-0000:00:14.0",
        "usb-0:3:1.0",
    ]


def test_split_dev_path_falls_back_to_single_segment():
    from hil_controller.adapters.usb_scan import split_dev_path

    # no recognised bus-type keyword -> one segment, unchanged
    assert split_dev_path("weird_name_no_type") == ["weird_name_no_type"]
    assert split_dev_path("") == [""]


def test_parse_dev_links_ignores_unknown_bases_and_malformed():
    from hil_controller.adapters.usb_scan import parse_dev_links

    grouped = parse_dev_links(
        "/dev/bogus/by-id\tx\t/dev/null\n"  # unknown base
        "no tabs here\n"  # malformed
        "\t\t\n"  # empty name
    )
    assert all(v == [] for v in grouped.values())
    assert set(grouped) == {
        "serial_by_id",
        "serial_by_path",
        "disk_by_id",
        "disk_by_label",
        "disk_by_path",
    }


# -- learn_once ----------------------------------------------------------


@pytest.mark.asyncio
async def test_learn_once_adds_unseen_id(app):
    from hil_controller.adapters.usb_scan import learn_once

    # Scan returns a NEW vid/pid on the device's hub_port_path
    fake_scan = lambda: [
        {"busid": "1-1.1.3", "vid": "239a", "pid": "0035", "description": "UF2 Bootloader"},
    ]
    added = await learn_once(
        app.state._test_db,
        device_id="pyportal",
        hub_port_path="1-1.1.3",
        job_id="job-123",
        scan_fn=fake_scan,
    )
    assert added == 1

    async with aiosqlite.connect(app.state._test_db) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT vid, pid, source, learned_from_job FROM device_usb_ids "
            "WHERE device_id='pyportal' AND pid='0035'"
        ) as cur:
            row = await cur.fetchone()
    assert row is not None
    assert row["source"] == "passive"
    assert row["learned_from_job"] == "job-123"


@pytest.mark.asyncio
async def test_learn_once_ignores_other_busids(app):
    from hil_controller.adapters.usb_scan import learn_once

    fake_scan = lambda: [
        {"busid": "1-1.99.99", "vid": "dead", "pid": "beef", "description": "off-port"},
    ]
    added = await learn_once(
        app.state._test_db,
        device_id="pyportal",
        hub_port_path="1-1.1.3",
        job_id="job-1",
        scan_fn=fake_scan,
    )
    assert added == 0


@pytest.mark.asyncio
async def test_learn_once_refreshes_existing_last_seen(app):
    from hil_controller.adapters.usb_scan import learn_once

    # Seeded device already has (239a, 8053); a scan that matches it
    # should NOT add a new row, but should bump last_seen_at.
    async with aiosqlite.connect(app.state._test_db) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT last_seen_at FROM device_usb_ids WHERE device_id='pyportal' AND pid='8053'"
        ) as cur:
            before = (await cur.fetchone())["last_seen_at"]

    import time

    time.sleep(0.01)
    added = await learn_once(
        app.state._test_db,
        device_id="pyportal",
        hub_port_path="1-1.1.3",
        job_id="job-x",
        scan_fn=lambda: [
            {"busid": "1-1.1.3", "vid": "239a", "pid": "8053", "description": "WipperSnapper"},
        ],
    )
    assert added == 0

    async with aiosqlite.connect(app.state._test_db) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT last_seen_at FROM device_usb_ids WHERE device_id='pyportal' AND pid='8053'"
        ) as cur:
            after = (await cur.fetchone())["last_seen_at"]
    assert after > before


@pytest.mark.asyncio
async def test_learn_once_handles_scan_failure_gracefully(app):
    from hil_controller.adapters.usb_scan import learn_once

    def broken():
        raise RuntimeError("ssh down")

    # Should not raise — return 0 added.
    added = await learn_once(
        app.state._test_db,
        device_id="pyportal",
        hub_port_path="1-1.1.3",
        job_id="job-1",
        scan_fn=broken,
    )
    assert added == 0
