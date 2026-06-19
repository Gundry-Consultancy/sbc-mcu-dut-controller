"""Uf2MscFlasher tests — no hardware, transport is a fake routing mock.

Locks the by-path MSC location, the mount→cp→sync→umount sequence, and the
1200-baud bootloader-entry behaviour.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hil_controller.adapters.flashers import Artifact, Uf2MscFlasher
from hil_controller.adapters.flashers.uf2_msc import usb_path_token
from hil_controller.hosts.base import ExecResult

PORT = "/dev/serial/by-path/platform-3f980000.usb-usb-0:1.1.4:1.0"
MSC_DEV = "/dev/sda"
INFO_UF2 = (
    "UF2 Bootloader v3.15.0 SFHWRO\nModel: PyPortal M4 Express\nBoard-ID: SAMD51J20A-PyPortal-v0\n"
)


def _result(exit_status: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    r = MagicMock(spec=ExecResult)
    r.exit_status = exit_status
    r.stdout = stdout
    r.stderr = stderr
    return r


class _RoutingTransport:
    """Fake transport routing exec() by argv; records calls. MSC present by default."""

    def __init__(self, *, msc_present: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.msc_present = msc_present

    async def exec(self, argv, *, cwd=None, env=None):  # noqa: ANN001
        self.calls.append(list(argv))
        a = argv[1:] if argv and argv[0] == "sudo" else argv
        joined = " ".join(a)
        # by-path scsi glob → resolve to /dev/sda when "present"
        if a[:2] == ["bash", "-c"] and "by-path" in joined and "-scsi-" in joined:
            return _result(0, stdout=(MSC_DEV + "\n") if self.msc_present else "")
        if a[:2] == ["bash", "-c"] and "INFO_UF2.TXT" in joined:
            return _result(0, stdout=INFO_UF2)
        if a[:2] == ["bash", "-c"] and "stat -c %s" in joined:
            return _result(0, stdout="410224\n")
        return _result(0)

    def cmds(self) -> list[str]:
        return [" ".join(c[1:] if c and c[0] == "sudo" else c) for c in self.calls]

    def has_sudo(self, needle: str) -> bool:
        return any(c and c[0] == "sudo" and needle in " ".join(c) for c in self.calls)


def test_usb_path_token() -> None:
    assert usb_path_token(PORT) == "1.1.4"
    assert usb_path_token("/dev/ttyACM0") is None


@pytest.mark.asyncio
async def test_is_in_bootloader_true_when_msc_present() -> None:
    f = Uf2MscFlasher(transport=_RoutingTransport(msc_present=True), port=PORT)
    assert await f.is_in_bootloader() is True


@pytest.mark.asyncio
async def test_is_in_bootloader_false_when_absent() -> None:
    f = Uf2MscFlasher(transport=_RoutingTransport(msc_present=False), port=PORT)
    assert await f.is_in_bootloader() is False


@pytest.mark.asyncio
async def test_flash_mounts_copies_syncs_unmounts() -> None:
    tp = _RoutingTransport(msc_present=True)
    f = Uf2MscFlasher(transport=tp, port=PORT, mount_dir="/tmp/hil-uf2mnt")
    res = await f.flash(Artifact(path="/tmp/fw.uf2", kind="uf2"))
    cmds = tp.cmds()
    assert any(c == f"mount {MSC_DEV} /tmp/hil-uf2mnt" for c in cmds), cmds
    assert any("cp /tmp/fw.uf2 /tmp/hil-uf2mnt/ && sync" in c for c in cmds), cmds
    assert any(c == "umount /tmp/hil-uf2mnt" for c in cmds), cmds
    # mount/cp/umount run under sudo (root needed to mount).
    assert tp.has_sudo(f"mount {MSC_DEV}")
    assert res.bytes_written == 410224


@pytest.mark.asyncio
async def test_flash_raises_when_no_msc_and_cannot_enter() -> None:
    tp = _RoutingTransport(msc_present=False)
    f = Uf2MscFlasher(transport=tp, port=PORT, settle_s=0)
    with pytest.raises(Exception):  # FlasherError — drive never appears
        await f.flash(Artifact(path="/tmp/fw.uf2"))


@pytest.mark.asyncio
async def test_probe_reads_board_id() -> None:
    f = Uf2MscFlasher(transport=_RoutingTransport(msc_present=True), port=PORT)
    info = await f.probe()
    assert info.family == "SAMD51J20A-PyPortal-v0"


@pytest.mark.asyncio
async def test_reset_bootloader_issues_1200_touch() -> None:
    tp = _RoutingTransport()
    f = Uf2MscFlasher(transport=tp, port=PORT, settle_s=0)
    await f.reset(into="bootloader")
    assert any("stty" in c and "1200" in c for c in tp.calls), tp.calls


@pytest.mark.asyncio
async def test_erase_is_noop() -> None:
    tp = _RoutingTransport()
    f = Uf2MscFlasher(transport=tp, port=PORT)
    await f.erase()  # no exception, no commands
    assert tp.calls == []


@pytest.mark.asyncio
async def test_enter_bootloader_touches_until_present() -> None:
    tp = _RoutingTransport(msc_present=False)
    # MSC appears after the first touch.
    seq = {"n": 0}
    orig = tp.exec

    async def exec_(argv, *, cwd=None, env=None):  # noqa: ANN001
        a = argv[1:] if argv and argv[0] == "sudo" else argv
        joined = " ".join(a)
        if a[:2] == ["bash", "-c"] and "by-path" in joined and "-scsi-" in joined:
            tp.calls.append(list(argv))
            present = seq["n"] >= 1
            return _result(0, stdout=(MSC_DEV + "\n") if present else "")
        if a[:1] == ["stty"]:
            seq["n"] += 1
        return await orig(argv, cwd=cwd, env=env)

    tp.exec = exec_  # type: ignore[assignment]
    f = Uf2MscFlasher(transport=tp, port=PORT, settle_s=0)
    await f.enter_bootloader(attempts=4)
    assert any("stty" in c and "1200" in c for c in tp.calls), tp.calls
