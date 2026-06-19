"""Tests for FirmwareBenchAdapter orchestration + hold loop.

The protomq launcher, serial capture, and stage pipeline are stubbed so the
test exercises the adapter's sequencing, live-log plumbing, and window logic
without real hardware/node/git.
"""

from __future__ import annotations

import asyncio
from datetime import UTC
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import hil_controller.adapters.firmware_bench as fb
from hil_controller.adapters.firmware_bench import (
    FirmwareBenchAdapter,
    _msc_filter_from_serial,
    _parse_offset,
)
from hil_controller.hosts.base import ExecResult


def _result(exit_status: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    r = MagicMock(spec=ExecResult)
    r.exit_status = exit_status
    r.stdout = stdout
    r.stderr = stderr
    return r


def _dut_transport() -> AsyncMock:
    t = AsyncMock()
    t.exec = AsyncMock(return_value=_result(0))
    t.copy_to = AsyncMock(return_value=None)
    return t


class _FakeProtomq:
    """Stands in for ProtomqLauncher — no clone/build/process."""

    def __init__(self, **kw: Any) -> None:
        self.on_line = kw.get("on_line")
        self.mqtt_port = 1884
        self.api_port = 5173
        self.ws_port = 8888
        self.started = False
        self.stopped = False

    async def clone_and_build(self) -> None:
        return None

    async def start(self, **kw: Any) -> None:
        self.started = True
        if self.on_line:
            self.on_line("MQTT listening on port 1884")

    async def stop(self) -> None:
        self.stopped = True


class _FakeSerial:
    def __init__(self, **kw: Any) -> None:
        self.on_line = kw.get("on_line")
        self.serial_path = kw.get("serial_path")
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


@pytest.fixture(autouse=True)
def _patch_collaborators(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(fb, "ProtomqLauncher", _FakeProtomq)
    monkeypatch.setattr(fb, "SerialCaptureAdapter", _FakeSerial)
    # run_stages: record the stages + context, and fire the two orchestrator
    # callbacks the way the real launch_protomq / start_serial_log handlers do
    # (protomq + serial now spin up mid-pipeline, not in flash() directly).
    calls: dict[str, Any] = {}

    async def _fake_run_stages(stages, ctx):
        calls["stages"] = stages
        calls["ctx"] = ctx
        for s in stages:
            if s.get("type") == "launch_protomq" and ctx.launch_protomq is not None:
                ctx.protomq_host, ctx.protomq_port = await ctx.launch_protomq()
            elif s.get("type") == "start_serial_log" and ctx.start_serial is not None:
                await ctx.start_serial()

    monkeypatch.setattr(fb, "run_stages", _fake_run_stages)
    monkeypatch.setattr(fb, "validate_stages", lambda s: None)
    return calls


def _adapter(tmp_path: Path, **params_over: Any) -> FirmwareBenchAdapter:
    bin_path = tmp_path / "combined.bin"
    bin_path.write_bytes(b"\x00" * 16)
    params = {
        "firmware": {"path": str(bin_path), "offset": "0x0"},
        "window_minutes": 30,
        "hold_poll_s": 0.01,
        "flash_serial_port": "/dev/serial/by-id/usb-flash-if00",
        "log_serial_port": "/dev/serial/by-id/usb-app-if00",
        "msc_filter": "QT_Py",
        **params_over,
    }
    return FirmwareBenchAdapter(
        controller_transport=AsyncMock(),
        dut_transport=_dut_transport(),
        hub_transport=_dut_transport(),
        job_id="job-1",
        device={"id": "dut-1", "serial_port": "/dev/ttyACM0", "solenoid_channel": 2},
        params=params,
        secrets={"IO_USERNAME": "u", "IO_KEY": "k"},
        controller_ip="192.168.1.169",
    )


# --------------------------------------------------------------------------- #
# pure helper                                                                 #
# --------------------------------------------------------------------------- #


def test_parse_offset_accepts_hex_and_int() -> None:
    assert _parse_offset("0x10000") == 65536
    assert _parse_offset(4096) == 4096
    assert _parse_offset(None) == 0


def test_msc_filter_derived_from_by_path_serial() -> None:
    # The QT Py's CDC serial and its MSC volume share the hub-port by-path.
    assert (
        _msc_filter_from_serial("/dev/serial/by-path/platform-3f980000.usb-usb-0:1.2:1.0")
        == "usb-0:1.2:"
    )
    # Deeper hub chain.
    assert (
        _msc_filter_from_serial("/dev/serial/by-path/platform-xhci.usb-usb-0:1.1.3:1.0")
        == "usb-0:1.1.3:"
    )
    # No by-path (by-id or empty) → no derivation.
    assert _msc_filter_from_serial("/dev/serial/by-id/usb-Adafruit_QT_Py-if00") == ""
    assert _msc_filter_from_serial("") == ""


def test_build_stages_injects_protomq_serial_and_bootlog(tmp_path: Path) -> None:
    # Full-loop stage list: protomq must land after erase / before flash; serial
    # before the first power_cycle; boot-log after the last power_cycle.
    a = _adapter(
        tmp_path,
        stages=[
            {"type": "enter_bootloader"},
            {"type": "erase", "before": "no_reset", "after": "no_reset"},
            {"type": "flash", "offset": "0x0"},
            {"type": "verify"},
            {"type": "power_cycle"},
            {"type": "write_secrets_msc"},
            {"type": "power_cycle"},
        ],
    )
    types = [s["type"] for s in a._build_stages(log_port="/dev/serial/by-id/x")]
    assert types.index("erase") < types.index("launch_protomq") < types.index("flash")
    assert types.index("start_serial_log") < types.index("power_cycle")
    assert types[-1] == "print_boot_log"  # right after the final power_cycle


def test_build_stages_no_protomq_or_bootlog_for_flash_only(tmp_path: Path) -> None:
    # No secrets stage, no power_cycle → no protomq, no serial-start, no boot-log.
    a = _adapter(
        tmp_path,
        stages=[
            {"type": "enter_bootloader"},
            {"type": "flash", "offset": "0x0"},
            {"type": "verify"},
        ],
    )
    types = [s["type"] for s in a._build_stages(log_port="/dev/serial/by-id/x")]
    assert "launch_protomq" not in types
    assert "start_serial_log" not in types
    assert "print_boot_log" not in types


# --------------------------------------------------------------------------- #
# flash phase                                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_flash_stages_firmware_launches_protomq_and_starts_serial(
    tmp_path: Path, _patch_collaborators
) -> None:
    emitted: list[tuple[str, dict]] = []

    async def _emit(kind, payload):
        emitted.append((kind, payload))

    a = _adapter(tmp_path, launch_protomq=True)
    a.bind_runtime(emit=_emit, db_path=None, job_id="job-1")
    await a.flash({"kind": "firmware-bin"})

    # firmware staged onto the DUT host
    a.dut_transport.copy_to.assert_awaited()
    # protomq launched, and its port handed to the stage context
    assert a._protomq.started is True
    ctx = _patch_collaborators["ctx"]
    assert ctx.protomq_host == "192.168.1.169"
    assert ctx.protomq_port == 1884
    assert ctx.flash_serial_port == "/dev/serial/by-id/usb-flash-if00"
    assert ctx.log_serial_port == "/dev/serial/by-id/usb-app-if00"
    assert ctx.msc_filter == "QT_Py"
    # serial capture attached to the post-reboot *log* port
    assert a._serial.started is True
    assert a._serial.serial_path == "/dev/serial/by-id/usb-app-if00"

    # release() flushes the queued log lines through _emit
    await a.release()
    streams = {p.get("stream") for k, p in emitted if k == "log"}
    assert "protomq" in streams and "bench" in streams


@pytest.mark.asyncio
async def test_flash_without_firmware_path_raises(tmp_path: Path) -> None:
    a = _adapter(tmp_path)
    a._fw_local_path = ""
    a.bind_runtime(emit=AsyncMock(), db_path=None, job_id="job-1")
    with pytest.raises(RuntimeError, match="no firmware"):
        await a.flash({})
    await a.release()


# --------------------------------------------------------------------------- #
# hold loop                                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_hold_loop_exits_when_window_status_returns_reason(tmp_path: Path) -> None:
    a = _adapter(tmp_path)
    a.bind_runtime(emit=AsyncMock(), db_path=None, job_id="job-1")
    a._drain_task = asyncio.create_task(a._drain_logs())
    # First poll says "keep holding", second ends it.
    a._window_status = AsyncMock(side_effect=[None, "window expired"])  # type: ignore[method-assign]
    result = await asyncio.wait_for(a.run(), timeout=2.0)
    assert result == "pass"
    assert a._window_status.await_count == 2
    await a.release()


@pytest.mark.asyncio
async def test_window_status_detects_cancelled_state(tmp_path: Path, monkeypatch) -> None:
    a = _adapter(tmp_path)
    a.bind_runtime(emit=AsyncMock(), db_path="/fake.db", job_id="job-1")

    # Fake get_job → cancelled; the loop should end immediately.
    import hil_controller.db.connection as dbconn

    class _FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(dbconn, "get_db", lambda _p: _FakeDB())

    async def _get_job(_db, _jid):
        return {"state": "cancelled"}

    monkeypatch.setattr(dbconn, "get_job", _get_job)

    reason = await a._window_status(deadline_monotonic=1e18)
    assert reason == "job state cancelled"


@pytest.mark.asyncio
async def test_window_status_expired_lease(tmp_path: Path, monkeypatch) -> None:
    from datetime import datetime, timedelta, timezone

    a = _adapter(tmp_path)
    a.bind_runtime(emit=AsyncMock(), db_path="/fake.db", job_id="job-1")

    import hil_controller.db.connection as dbconn
    from hil_controller.queue import leases

    class _FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(dbconn, "get_db", lambda _p: _FakeDB())
    monkeypatch.setattr(dbconn, "get_job", AsyncMock(return_value={"state": "running"}))
    past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    monkeypatch.setattr(
        leases, "get_active_for_job", AsyncMock(return_value={"id": 1, "expires_at": past})
    )

    assert await a._window_status(1e18) == "window expired"


@pytest.mark.asyncio
async def test_teardown_stops_protomq_and_serial(tmp_path: Path, _patch_collaborators) -> None:
    a = _adapter(tmp_path, launch_protomq=True)
    a.bind_runtime(emit=AsyncMock(), db_path=None, job_id="job-1")
    await a.flash({"kind": "firmware-bin"})
    proto, serial = a._protomq, a._serial
    await a.release()
    assert proto.stopped is True
    assert serial.stopped is True


@pytest.mark.asyncio
async def test_teardown_registers_serial_and_protomq_log_assets(
    tmp_path: Path, _patch_collaborators
) -> None:
    import sqlite3

    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE assets (id TEXT PRIMARY KEY, filename TEXT, path TEXT, "
        "size_bytes INTEGER, kind TEXT, job_id TEXT, created_at TEXT)"
    )
    conn.commit()
    conn.close()

    a = _adapter(tmp_path, launch_protomq=True)
    a.jobs_dir = str(tmp_path)
    a.bind_runtime(emit=AsyncMock(), db_path=str(db), job_id="job-1")
    await a.flash({"kind": "firmware-bin"})
    # Simulate captured serial output (FakeSerial doesn't write the file);
    # FakeProtomq.start() already wrote a protomq line via _protomq_line.
    a._serial_log_path.write_text("boot line 1\nboot line 2\n")
    await a.release()

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    names = {
        r["filename"]
        for r in conn.execute("SELECT filename FROM assets WHERE job_id='job-1' AND kind='log'")
    }
    conn.close()
    assert "serial.log" in names
    assert "protomq.log" in names


@pytest.mark.asyncio
async def test_protomq_skipped_without_secrets_stage(tmp_path: Path, _patch_collaborators) -> None:
    # Default stages have no write_secrets_msc and launch_protomq is unset →
    # protomq is never started, but serial capture still runs.
    a = _adapter(tmp_path)  # no launch_protomq
    a.bind_runtime(emit=AsyncMock(), db_path=None, job_id="job-1")
    await a.flash({"kind": "firmware-bin"})
    assert a._protomq is None
    assert a._serial.started is True
    ctx = _patch_collaborators["ctx"]
    assert ctx.protomq_port == 0
    await a.release()
