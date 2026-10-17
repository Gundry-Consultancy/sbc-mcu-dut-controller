"""CircuitPython transport and assertion behavior, using a fake bench."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hil_controller.adapters import circuitpython as cp
from hil_controller.adapters.bench_stages import BenchContext


@pytest.mark.parametrize(
    "path",
    [
        "../bad.py",
        "/bad.py",
        "lib/../bad.py",
        "a\\b.py",
        "C:foo",
        "settings.toml",
        "boot.py",
        "lib//a.py",
    ],
)
def test_invalid_file_paths(path):
    with pytest.raises(ValueError):
        cp.validate_files({"code.py": "", path: ""})


async def test_write_settings_files_order_and_cleanup(monkeypatch):
    transport = AsyncMock()
    transport.exec.return_value = SimpleNamespace(exit_status=0, stdout="", stderr="")
    monkeypatch.setattr(cp, "resolve_msc_device", AsyncMock(return_value="/dev/dut"))
    monkeypatch.setattr(
        cp, "_mount_msc", AsyncMock(return_value=("/dev/dut", "/media/CIRCUITPY", ["unmount"]))
    )
    ctx = BenchContext(
        transport,
        transport,
        "/dev/tty",
        secrets={
            "WIFI_SSID": "free4all",
            "WIFI_PASSWORD": "password",
            "IO_KEY": 'example"\nkey',
        },
        protomq_host="bench",
        protomq_port=1884,
    )
    await cp.write_circuitpython({"files": {"code.py": "print('ok')", "lib/helper.py": "x=1"}}, ctx)
    calls = transport.exec.call_args_list
    writes = [c for c in calls if c.args[0][0] == "python3"]
    assert writes[-1].args[0][-1].endswith("/code.py")
    settings = next(
        c.kwargs["stdin"].decode() for c in writes if c.args[0][-1].endswith("settings.toml")
    )
    import tomllib

    assert tomllib.loads(settings)["ADAFRUIT_IO_KEY"] == 'example"\nkey'
    assert calls[-1].args[0] == ["unmount"]
    assert 'example"' not in ctx.transcript_text()


async def test_write_failure_unmounts(monkeypatch):
    transport = AsyncMock()
    transport.exec.return_value = SimpleNamespace(exit_status=1, stdout="", stderr="failed")
    monkeypatch.setattr(cp, "resolve_msc_device", AsyncMock(return_value="/dev/dut"))
    monkeypatch.setattr(
        cp, "_mount_msc", AsyncMock(return_value=("/dev/dut", "/media/cpy", ["unmount"]))
    )
    ctx = BenchContext(transport, transport, "/dev/tty", protomq_host="bench", protomq_port=1884)
    with pytest.raises(RuntimeError, match="write failed"):
        await cp.write_circuitpython({"files": {"code.py": ""}}, ctx)
    assert transport.exec.call_args.args[0] == ["unmount"]


async def test_serial_success_and_timeout(tmp_path):
    path = tmp_path / "serial.log"
    path.write_text("boot\nHIL_TEST_OK\n")
    ctx = BenchContext(AsyncMock(), AsyncMock(), "/dev/tty", serial_log_path=str(path))
    await cp.assert_serial({"contains": "HIL_TEST_OK"}, ctx)
    with pytest.raises(RuntimeError, match="not observed"):
        await cp.assert_serial({"contains": "missing", "timeout_s": 0.01}, ctx)


async def test_serial_checkpoint_rejects_old_success(tmp_path):
    path = tmp_path / "serial.log"
    path.write_text("OLD_SUCCESS\n")
    ctx = BenchContext(AsyncMock(), AsyncMock(), "/dev/tty", serial_log_path=str(path))
    await cp.serial_checkpoint({}, ctx)
    with pytest.raises(RuntimeError, match="not observed"):
        await cp.assert_serial({"contains": "OLD_SUCCESS", "timeout_s": 0.01}, ctx)
    with path.open("a") as stream:
        stream.write("NEW_SUCCESS\n")
    await cp.assert_serial({"contains": "NEW_SUCCESS"}, ctx)


async def test_storage_enumeration_retries_before_writing(monkeypatch):
    transport = AsyncMock()
    transport.exec.return_value = SimpleNamespace(exit_status=0, stdout="", stderr="")
    resolve = AsyncMock(side_effect=[cp.MscError("not ready"), "/dev/dut-part1"])
    monkeypatch.setattr(cp, "resolve_msc_device", resolve)
    monkeypatch.setattr(cp.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(
        cp, "_mount_msc", AsyncMock(return_value=("/dev/dut-part1", "/media/cpy", ["unmount"]))
    )
    ctx = BenchContext(transport, transport, "/dev/tty", protomq_host="bench", protomq_port=1884)
    await cp.write_circuitpython({"files": {"code.py": "print('ok')"}}, ctx)
    assert resolve.await_count == 2
    assert transport.exec.call_args.args[0] == ["unmount"]


async def test_storage_enumeration_timeout_does_not_write(monkeypatch):
    transport = AsyncMock()
    monkeypatch.setattr(cp, "resolve_msc_device", AsyncMock(side_effect=cp.MscError("absent")))
    monkeypatch.setattr(cp, "time", SimpleNamespace(monotonic=iter([0, 31]).__next__))
    ctx = BenchContext(transport, transport, "/dev/tty", protomq_host="bench", protomq_port=1884)
    with pytest.raises(cp.MscError, match="absent"):
        await cp.write_circuitpython({"files": {"code.py": ""}}, ctx)
    transport.exec.assert_not_called()
