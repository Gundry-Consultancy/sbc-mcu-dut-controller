"""Tests for the MSC secrets writer (resolve → mount → write → unmount)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from hil_controller.adapters.msc_secrets import (
    MscError,
    parse_udisks_mountpoint,
    read_msc_files,
    render_secrets_json,
    resolve_msc_device,
    select_block_device,
    write_secrets_to_msc,
)
from hil_controller.hosts.base import ExecResult


def _result(exit_status: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    r = MagicMock(spec=ExecResult)
    r.exit_status = exit_status
    r.stdout = stdout
    r.stderr = stderr
    return r


# --------------------------------------------------------------------------- #
# parse_udisks_mountpoint                                                     #
# --------------------------------------------------------------------------- #


def test_parse_mountpoint_with_trailing_dot() -> None:
    assert parse_udisks_mountpoint("Mounted /dev/sda at /media/pi/WIPPER.") == "/media/pi/WIPPER"


def test_parse_mountpoint_without_trailing_dot() -> None:
    assert (
        parse_udisks_mountpoint("Mounted /dev/sdb at /run/media/pi/CIRCUITPY")
        == "/run/media/pi/CIRCUITPY"
    )


def test_parse_mountpoint_none_when_absent() -> None:
    assert parse_udisks_mountpoint("some error happened") is None
    assert parse_udisks_mountpoint("") is None


# --------------------------------------------------------------------------- #
# select_block_device                                                         #
# --------------------------------------------------------------------------- #

_BY_ID = [
    "usb-Adafruit_QT_Py_ESP32-S3_4MB_Flash_2MB_PS-0:0",
    "usb-Adafruit_QT_Py_ESP32-S3_4MB_Flash_2MB_PS-0:0-part1",
    "usb-Generic_Flash_Disk_ABCD-0:0",
]


def test_select_substring_match_prefers_whole_disk_over_partition() -> None:
    got = select_block_device(_BY_ID, "QT_Py_ESP32-S3")
    assert got == "usb-Adafruit_QT_Py_ESP32-S3_4MB_Flash_2MB_PS-0:0"


def test_select_glob_match() -> None:
    got = select_block_device(_BY_ID, "usb-Adafruit_QT_Py*-0:0")
    assert got == "usb-Adafruit_QT_Py_ESP32-S3_4MB_Flash_2MB_PS-0:0"


def test_select_case_insensitive() -> None:
    assert select_block_device(_BY_ID, "generic_flash") == "usb-Generic_Flash_Disk_ABCD-0:0"


def test_select_no_match_returns_none() -> None:
    assert select_block_device(_BY_ID, "Raspberry") is None
    assert select_block_device(_BY_ID, "") is None


# --------------------------------------------------------------------------- #
# render_secrets_json                                                         #
# --------------------------------------------------------------------------- #


def test_render_emits_io_port_as_number_and_includes_wifi() -> None:
    out = render_secrets_json(
        io_url="192.168.1.169",
        io_port=1884,
        io_username="bench",
        io_key="abc",
        wifi_ssid="HILNET",
        wifi_password="pw",
    )
    data = json.loads(out)
    assert data["io_url"] == "192.168.1.169"
    assert data["io_port"] == 1884 and isinstance(data["io_port"], int)
    assert data["io_username"] == "bench"
    assert data["network_type_wifi"] == {"network_ssid": "HILNET", "network_password": "pw"}


def test_render_omits_wifi_block_when_no_ssid() -> None:
    data = json.loads(render_secrets_json(io_url="h", io_port=1885))
    assert "network_type_wifi" not in data
    assert data["io_port"] == 1885


# --------------------------------------------------------------------------- #
# resolve_msc_device                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_resolve_empty_filter_raises() -> None:
    tp = AsyncMock()
    with pytest.raises(MscError, match="no MSC filter"):
        await resolve_msc_device(tp, "")


@pytest.mark.asyncio
async def test_resolve_prefers_by_path() -> None:
    # by-path is searched first; a by-path port filter resolves there.
    bypath = ["platform-3f980000.usb-usb-0:1.2:1.2-scsi-0:0:0:0"]

    async def _exec(argv, **kw):
        if argv[:2] == ["ls", "-1"] and argv[2] == "/dev/disk/by-path":
            return _result(0, stdout="\n".join(bypath))
        if argv[:2] == ["ls", "-1"] and argv[2] == "/dev/disk/by-id":
            return _result(0, stdout="\n".join(_BY_ID))
        return _result(0)

    tp = AsyncMock()
    tp.exec = AsyncMock(side_effect=_exec)
    got = await resolve_msc_device(tp, "usb-0:1.2:")
    assert got == "/dev/disk/by-path/platform-3f980000.usb-usb-0:1.2:1.2-scsi-0:0:0:0"


@pytest.mark.asyncio
async def test_resolve_falls_back_to_by_id() -> None:
    # Nothing in by-path matches; a by-id substring filter still resolves.
    async def _exec(argv, **kw):
        if argv[2] == "/dev/disk/by-path":
            return _result(0, stdout="platform-x-scsi-0:0:0:0")
        return _result(0, stdout="\n".join(_BY_ID))

    tp = AsyncMock()
    tp.exec = AsyncMock(side_effect=_exec)
    got = await resolve_msc_device(tp, "QT_Py_ESP32-S3")
    assert got == "/dev/disk/by-id/usb-Adafruit_QT_Py_ESP32-S3_4MB_Flash_2MB_PS-0:0"


@pytest.mark.asyncio
async def test_resolve_no_match_raises_with_seen_list() -> None:
    tp = AsyncMock()
    tp.exec = AsyncMock(return_value=_result(0, stdout="\n".join(_BY_ID)))
    with pytest.raises(MscError, match="no /dev/disk entry matched"):
        await resolve_msc_device(tp, "Raspberry")


# --------------------------------------------------------------------------- #
# write_secrets_to_msc (full flow)                                            #
# --------------------------------------------------------------------------- #


def _flow_transport(write_exit: int = 0) -> AsyncMock:
    """Transport that responds per-command for the mount/write/unmount flow."""

    async def _exec(argv, **kwargs):
        if argv[:2] == ["ls", "-1"]:
            return _result(0, stdout="\n".join(_BY_ID))
        if argv[:3] == ["udisksctl", "mount", "-b"]:
            return _result(0, stdout="Mounted /dev/sda at /media/pi/WIPPER.")
        if argv[0] == "tee":
            return _result(write_exit, stderr="" if write_exit == 0 else "read-only fs")
        if argv == ["sync"]:
            return _result(0)
        if argv[:3] == ["udisksctl", "unmount", "-b"]:
            return _result(0)
        return _result(0)

    tp = AsyncMock()
    tp.exec = AsyncMock(side_effect=_exec)
    return tp


@pytest.mark.asyncio
async def test_write_flow_argv_sequence_and_stdin() -> None:
    tp = _flow_transport()
    dev, mnt = await write_secrets_to_msc(
        tp,
        msc_filter="QT_Py_ESP32-S3",
        secrets_json='{"io_port": 1884}\n',
    )
    # _flow_transport returns the same names for any disk dir; by-path is searched
    # first, so the resolved device comes from there.
    assert dev == "/dev/disk/by-path/usb-Adafruit_QT_Py_ESP32-S3_4MB_Flash_2MB_PS-0:0"
    assert mnt == "/media/pi/WIPPER"

    calls = tp.exec.call_args_list
    argvs = [c.args[0] for c in calls]
    assert ["ls", "-1", "/dev/disk/by-path"] in argvs
    assert ["udisksctl", "mount", "-b", dev] in argvs
    # secrets written via tee with the body on stdin to <mountpoint>/secrets.json
    tee_call = next(c for c in calls if c.args[0][0] == "tee")
    assert tee_call.args[0] == ["tee", "/media/pi/WIPPER/secrets.json"]
    assert tee_call.kwargs["stdin"] == b'{"io_port": 1884}\n'
    assert ["sync"] in argvs
    assert ["udisksctl", "unmount", "-b", dev] in argvs


@pytest.mark.asyncio
async def test_write_flow_unmounts_even_when_write_fails() -> None:
    tp = _flow_transport(write_exit=1)
    with pytest.raises(MscError, match="failed"):
        await write_secrets_to_msc(tp, msc_filter="QT_Py_ESP32-S3", secrets_json="{}")
    argvs = [c.args[0] for c in tp.exec.call_args_list]
    # the finally-block unmount still ran
    assert any(a[:3] == ["udisksctl", "unmount", "-b"] for a in argvs)


@pytest.mark.asyncio
async def test_write_flow_falls_back_to_sudo_mount_when_udisksctl_unauthorized() -> None:
    # udisksctl returns NotAuthorized (the Pi-over-SSH polkit case) → sudo mount.
    async def _exec(argv, **kwargs):
        if argv[:2] == ["ls", "-1"]:
            return _result(0, stdout="\n".join(_BY_ID))
        if argv[:3] == ["udisksctl", "mount", "-b"]:
            return _result(1, stderr="NotAuthorized")
        if argv[:2] == ["readlink", "-f"]:
            return _result(0, stdout="/dev/sda")
        return _result(0)

    tp = AsyncMock()
    tp.exec = AsyncMock(side_effect=_exec)
    dev, mnt = await write_secrets_to_msc(tp, msc_filter="QT_Py_ESP32-S3", secrets_json="{}")
    assert dev == "/dev/sda" and mnt == "/tmp/hil-msc-sda"
    cmds = [
        " ".join(c.args[0]) if isinstance(c.args[0], list) else c.args[0]
        for c in tp.exec.call_args_list
    ]
    blob = "\n".join(c if isinstance(c, str) else " ".join(c) for c in cmds)
    assert "sudo mount -t vfat -o rw,uid=$(id -u),gid=$(id -g) /dev/sda" in blob
    assert "sudo umount" in blob


@pytest.mark.asyncio
async def test_read_msc_files_parses_boot_logs() -> None:
    # Read-only mount goes straight to sudo mount (udisksctl is rw-only).
    boot = "@@@FILE@@@ /tmp/hil-msc-sda/wipper_boot_out.txt\nWipperSnapper v1.0\nBoard: QT Py\n"

    async def _exec(argv, **kwargs):
        if argv[:2] == ["ls", "-1"]:
            return _result(0, stdout="\n".join(_BY_ID))
        if argv[:2] == ["readlink", "-f"]:
            return _result(0, stdout="/dev/sda")
        if argv[0] == "bash" and "nullglob" in argv[2]:
            return _result(0, stdout=boot)
        return _result(0)

    tp = AsyncMock()
    tp.exec = AsyncMock(side_effect=_exec)
    files = await read_msc_files(tp, msc_filter="QT_Py_ESP32-S3")
    assert any(k.endswith("wipper_boot_out.txt") for k in files)
    assert "WipperSnapper v1.0" in next(iter(files.values()))
    # mounted read-only, and the udisksctl rw path was NOT used for a read.
    blob = "\n".join(
        " ".join(c.args[0]) if isinstance(c.args[0], list) else c.args[0]
        for c in tp.exec.call_args_list
    )
    assert "-o ro" in blob
    assert "udisksctl mount" not in blob
