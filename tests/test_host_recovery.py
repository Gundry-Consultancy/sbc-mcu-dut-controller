"""Tests for wedged-host (dwc_otg) detection + auto-reboot recovery."""

from __future__ import annotations

from datetime import UTC

import pytest

from hil_controller import host_recovery as hr
from hil_controller.db.connection import get_db, init_db


async def _seed(db_path: str) -> None:
    async with get_db(db_path) as db:
        await db.execute(
            "INSERT INTO hosts (id, status) VALUES ('rpi-displays','available'), ('other','available')"  # noqa: E501
        )
        # two DUTs on rpi-displays (one by host_id, one by hub_host_id), one elsewhere
        await db.execute(
            "INSERT INTO devices (id, host_id, kind, status) VALUES "
            "('d-a','rpi-displays','microcontroller','available'),"
            "('d-elsewhere','other','microcontroller','available')"
        )
        await db.execute(
            "INSERT INTO devices (id, host_id, kind, status, hub_host_id) VALUES "
            "('d-b','some-pi','microcontroller','available','rpi-displays')"
        )
        await db.commit()


@pytest.mark.asyncio
async def test_mark_host_wedged_flags_host_and_its_devices(tmp_path):
    db_path = str(tmp_path / "hr.db")
    await init_db(db_path)
    await _seed(db_path)

    await hr.mark_host_wedged(db_path, "rpi-displays")

    async with get_db(db_path) as db:
        rows = {
            r["id"]: dict(r)
            for r in await (
                await db.execute("SELECT id, status, unavailable_kind FROM devices")
            ).fetchall()
        }
        host = dict(
            await (await db.execute("SELECT status FROM hosts WHERE id='rpi-displays'")).fetchone()
        )
    # both rpi-displays DUTs (by host_id and by hub_host_id) flagged; the other untouched
    assert rows["d-a"]["status"] == "unavailable" and rows["d-a"]["unavailable_kind"] == "temporary"
    assert rows["d-b"]["status"] == "unavailable"
    assert rows["d-elsewhere"]["status"] == "available"
    assert host["status"] == "reboot_required"


@pytest.mark.asyncio
async def test_mark_host_wedged_sets_retry_after_eta(tmp_path):
    """The flagged devices advertise retry_after = now + reboot_eta_s (for CI wait)."""
    from datetime import datetime, timezone

    db_path = str(tmp_path / "hr.db")
    await init_db(db_path)
    await _seed(db_path)

    reason = await hr.mark_host_wedged(db_path, "rpi-displays", reboot_eta_s=300)
    assert "back ~300s" in reason

    async with get_db(db_path) as db:
        rows = {
            r["id"]: dict(r)
            for r in await (await db.execute("SELECT id, retry_after FROM devices")).fetchall()
        }
    # both rpi-displays DUTs get a future retry_after ~300s out; the other has none
    for dev in ("d-a", "d-b"):
        ra = datetime.fromisoformat(rows[dev]["retry_after"])
        delta = (ra - datetime.now(UTC)).total_seconds()
        assert 240 <= delta <= 300, f"{dev} retry_after delta={delta}"
    assert rows["d-elsewhere"]["retry_after"] is None


@pytest.mark.asyncio
async def test_mark_host_unreachable_flags_temporary_without_reboot(tmp_path):
    """A network-unreachable host flags its devices temporary+retry_after but does
    NOT mark the host reboot_required (can't SSH-reboot an off-network box)."""
    from datetime import datetime, timezone

    db_path = str(tmp_path / "hr.db")
    await init_db(db_path)
    await _seed(db_path)

    reason = await hr.mark_host_unreachable(db_path, "rpi-displays", reboot_eta_s=300)
    assert "unreachable" in reason and "back ~300s" in reason

    async with get_db(db_path) as db:
        rows = {
            r["id"]: dict(r)
            for r in await (
                await db.execute("SELECT id, status, unavailable_kind, retry_after FROM devices")
            ).fetchall()
        }
        host = dict(
            await (await db.execute("SELECT status FROM hosts WHERE id='rpi-displays'")).fetchone()
        )
    for dev in ("d-a", "d-b"):
        assert rows[dev]["status"] == "unavailable" and rows[dev]["unavailable_kind"] == "temporary"
        ra = datetime.fromisoformat(rows[dev]["retry_after"])
        assert 240 <= (ra - datetime.now(UTC)).total_seconds() <= 300
    assert rows["d-elsewhere"]["status"] == "available"
    # NOT reboot_required — unlike a wedge (an off-network host can't be SSH-rebooted)
    assert host["status"] == "available"


@pytest.mark.asyncio
async def test_recover_wedged_hosts_disabled_only_logs(tmp_path):
    db_path = str(tmp_path / "hr.db")
    await init_db(db_path)
    await _seed(db_path)
    await hr.mark_host_wedged(db_path, "rpi-displays")
    calls: list[str] = []

    async def reboot_fn(host_id):
        calls.append(host_id)
        return True

    recovered = await hr.recover_wedged_hosts(db_path, reboot_fn=reboot_fn, enabled=False)
    assert recovered == [] and calls == []  # never reboots when disabled
    async with get_db(db_path) as db:
        host = dict(
            await (await db.execute("SELECT status FROM hosts WHERE id='rpi-displays'")).fetchone()
        )
    assert host["status"] == "reboot_required"  # left flagged


@pytest.mark.asyncio
async def test_recover_wedged_hosts_enabled_reboots_and_clears(tmp_path):
    db_path = str(tmp_path / "hr.db")
    await init_db(db_path)
    await _seed(db_path)
    await hr.mark_host_wedged(db_path, "rpi-displays")
    calls: list[str] = []

    async def reboot_fn(host_id):
        calls.append(host_id)
        return True

    recovered = await hr.recover_wedged_hosts(db_path, reboot_fn=reboot_fn, enabled=True)
    assert recovered == ["rpi-displays"] and calls == ["rpi-displays"]
    async with get_db(db_path) as db:
        rows = {
            r["id"]: dict(r)
            for r in await (await db.execute("SELECT id, status FROM devices")).fetchall()
        }
        host = dict(
            await (await db.execute("SELECT status FROM hosts WHERE id='rpi-displays'")).fetchone()
        )
    # devices cleared back, host available again
    assert rows["d-a"]["status"] == "available" and rows["d-b"]["status"] == "available"
    assert host["status"] == "available"


@pytest.mark.asyncio
async def test_recover_wedged_hosts_defers_while_jobs_in_flight(tmp_path):
    db_path = str(tmp_path / "hr.db")
    await init_db(db_path)
    await _seed(db_path)
    await hr.mark_host_wedged(db_path, "rpi-displays")
    async with get_db(db_path) as db:  # a job still running on the host
        await db.execute(
            "INSERT INTO jobs (id, request_json, state, assigned_host, created_at) "
            "VALUES ('j1','{}','running','rpi-displays','2026-06-15T00:00:00+00:00')"
        )
        await db.commit()
    calls: list[str] = []

    async def reboot_fn(host_id):
        calls.append(host_id)
        return True

    recovered = await hr.recover_wedged_hosts(db_path, reboot_fn=reboot_fn, enabled=True)
    assert recovered == [] and calls == []  # drains first — no reboot while a job runs


@pytest.mark.asyncio
async def test_reboot_host_command_sequence(tmp_path, monkeypatch):
    monkeypatch.setattr(hr.asyncio, "sleep", _noop_sleep)

    class _R:
        def __init__(self, s=0):
            self.exit_status = s

    class FakeTransport:
        def __init__(self):
            self.calls: list[list[str]] = []

        async def exec(self, argv):
            self.calls.append(argv)
            return _R(0)

    tp = FakeTransport()
    ok = await hr.reboot_host(tp, wait_back_s=4, poll_s=2)
    assert ok is True
    flat = [" ".join(c) for c in tp.calls]
    assert any("all_off" in c for c in flat)
    assert any(c == "sudo reboot" for c in flat)
    assert any(c == "true" for c in flat)  # waited for it back
    assert any("all_on" in c for c in flat)
    # ordering: all_off and reboot before all_on
    assert flat.index(next(c for c in flat if "all_off" in c)) < flat.index(
        next(c for c in flat if "all_on" in c)
    )


@pytest.mark.asyncio
async def test_reboot_host_times_out_if_never_back(tmp_path, monkeypatch):
    monkeypatch.setattr(hr.asyncio, "sleep", _noop_sleep)

    class FakeTransport:
        def __init__(self):
            self.calls: list[list[str]] = []

        async def exec(self, argv):
            self.calls.append(argv)
            if argv == ["true"]:
                raise OSError("still down")  # host never comes back
            return type("R", (), {"exit_status": 0})()

    tp = FakeTransport()
    ok = await hr.reboot_host(tp, wait_back_s=4, poll_s=2)
    assert ok is False
    assert not any("all_on" in " ".join(c) for c in tp.calls)  # never re-powered


@pytest.mark.asyncio
async def test_reboot_host_sequential_per_channel_bringup(monkeypatch):
    """With channel_nodes, recovery brings channels up ONE AT A TIME (turn_on per
    channel + presence check), never the all_on storm."""
    monkeypatch.setattr(hr.asyncio, "sleep", _noop_sleep)

    class _R:
        def __init__(self, s=0, out=""):
            self.exit_status = s
            self.stdout = out

    class FakeTransport:
        def __init__(self):
            self.calls: list[list[str]] = []

        async def exec(self, argv):
            self.calls.append(argv)
            joined = " ".join(argv)
            if "dmesg" in joined:
                return _R(0, "usb 1-1.2: new full-speed USB device number 5 using dwc_otg\n")
            return _R(0)

    tp = FakeTransport()
    ok = await hr.reboot_host(
        tp,
        channel_nodes=[(4, "/dev/serial/by-path/qtpy"), (1, "/dev/serial/by-path/f1")],
        wait_back_s=4,
        poll_s=2,
    )
    assert ok is True
    flat = [" ".join(c) for c in tp.calls]
    assert any("turn_on.sh 4" in c for c in flat) and any("turn_on.sh 1" in c for c in flat)
    assert not any("all_on" in c for c in flat)  # sequential, NOT the all_on storm
    assert any(c.startswith("test -e") for c in flat)  # presence-checked the nodes


@pytest.mark.asyncio
async def test_dmesg_usb_error_count_ignores_benign_enumeration():
    """Benign 'new/reset … using dwc_otg' lines are not errors; real -32/-110 are."""

    class _R:
        def __init__(self, out):
            self.exit_status = 0
            self.stdout = out

    benign = (
        "usb 1-1.2: new full-speed USB device number 5 using dwc_otg\n"
        "usb 1-1.4: reset full-speed USB device number 8 using dwc_otg\n"
    )
    storm = benign + ("usb 1-1.2: device descriptor read/all, error -32\nusb 1-1.3: error -110\n")

    class T:
        def __init__(self, out):
            self.out = out

        async def exec(self, argv):
            return _R(self.out)

    assert await hr.dmesg_usb_error_count(T(benign)) == 0
    assert await hr.dmesg_usb_error_count(T(storm)) == 2


@pytest.mark.asyncio
async def test_validate_presence_active_powers_on_checks_then_off(monkeypatch):
    """Active probe: turn_on ch → node present + dmesg clean → returns True, and
    ALWAYS turns the channel off again (idle)."""
    monkeypatch.setattr(hr.asyncio, "sleep", _noop_sleep)

    class _R:
        def __init__(self, s=0, out=""):
            self.exit_status = s
            self.stdout = out

    class FakeTransport:
        def __init__(self):
            self.calls: list[str] = []

        async def exec(self, argv):
            j = " ".join(argv)
            self.calls.append(j)
            if "dmesg" in j:
                return _R(0, "usb 1-1.2: new full-speed USB device number 5 using dwc_otg\n")
            if argv[:2] == ["test", "-e"]:
                return _R(0)  # node present
            return _R(0)

    tp = FakeTransport()
    ok = await hr.validate_presence_active(tp, channel=4, node="/dev/serial/by-path/qtpy")
    assert ok is True
    assert any("turn_on.sh 4" in c for c in tp.calls)
    assert any("turn_off.sh 4" in c for c in tp.calls)  # always de-energised after


@pytest.mark.asyncio
async def test_validate_presence_active_absent_node_off_and_false(monkeypatch):
    """Node never enumerates → False, and the channel is still turned off."""
    monkeypatch.setattr(hr.asyncio, "sleep", _noop_sleep)

    class _R:
        def __init__(self, s=0, out=""):
            self.exit_status = s
            self.stdout = out

    class FakeTransport:
        def __init__(self):
            self.calls: list[str] = []

        async def exec(self, argv):
            j = " ".join(argv)
            self.calls.append(j)
            if argv[:2] == ["test", "-e"]:
                return _R(1)  # node ABSENT
            return _R(0)

    tp = FakeTransport()
    ok = await hr.validate_presence_active(tp, channel=4, node="/dev/serial/by-path/qtpy")
    assert ok is False
    assert any("turn_off.sh 4" in c for c in tp.calls)  # de-energised even on failure


async def _noop_sleep(_s):  # fast tests — don't actually wait
    return None


# ---------------------------------------------------------------------------
# presence_node — resolving what the presence probe should `test -e`
# ---------------------------------------------------------------------------


def test_presence_node_prefers_serial_port() -> None:
    node = hr.presence_node(
        {
            "serial_port": "/dev/serial/by-path/platform-x.usb-usb-0:1.2:1.0",
            "hub_port_path": "1-1.2",
        }
    )
    assert node == "/dev/serial/by-path/platform-x.usb-usb-0:1.2:1.0"


def test_presence_node_maps_bare_busid_to_sysfs() -> None:
    """A busid-only device used to be probed as `test -e 1-1.1.3` — never true.
    It must map to the sysfs node that exists exactly while enumerated."""
    assert hr.presence_node({"serial_port": None, "hub_port_path": "1-1.1.3"}) == (
        "/sys/bus/usb/devices/1-1.1.3"
    )


def test_presence_node_none_when_no_fields() -> None:
    assert hr.presence_node({"serial_port": None, "hub_port_path": None}) is None
    assert hr.presence_node({}) is None
