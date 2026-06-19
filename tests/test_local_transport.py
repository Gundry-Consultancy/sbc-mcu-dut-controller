"""Tests for LocalTransport (subprocess-based local execution)."""

from pathlib import Path, PurePosixPath
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hil_controller.hosts.local import LocalTransport


def _make_proc(returncode=0, stdout=b"", stderr=b""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


@pytest.mark.asyncio
async def test_exec_captures_stdout():
    proc = _make_proc(0, stdout=b"hello\n")
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        result = await LocalTransport().exec(["echo", "hello"])
    assert result.exit_status == 0
    assert result.stdout == "hello\n"
    assert result.stderr == ""


@pytest.mark.asyncio
async def test_exec_nonzero_exit():
    proc = _make_proc(1, stderr=b"oops")
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        result = await LocalTransport().exec(["false"])
    assert result.exit_status == 1
    assert result.stderr == "oops"


@pytest.mark.asyncio
async def test_exec_passes_cwd():
    proc = _make_proc(0)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as mock_exec:
        await LocalTransport().exec(["ls"], cwd="/tmp")
    _, kwargs = mock_exec.call_args
    assert kwargs["cwd"] == "/tmp"


@pytest.mark.asyncio
async def test_exec_merges_env():
    import os

    proc = _make_proc(0)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as mock_exec:
        await LocalTransport().exec(["env"], env={"MY_VAR": "val"})
    _, kwargs = mock_exec.call_args
    assert kwargs["env"]["MY_VAR"] == "val"
    assert "PATH" in kwargs["env"]  # existing env preserved


@pytest.mark.asyncio
async def test_exec_passes_stdin():
    proc = _make_proc(0, stdout=b"hi")
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        result = await LocalTransport().exec(["cat"], stdin=b"hi")
    proc.communicate.assert_awaited_once_with(input=b"hi")
    assert result.stdout == "hi"


@pytest.mark.asyncio
async def test_copy_to(tmp_path: Path):
    src = tmp_path / "src.txt"
    src.write_text("data")
    dst = tmp_path / "dst.txt"
    await LocalTransport().copy_to(src, PurePosixPath(str(dst)))
    assert dst.read_text() == "data"


@pytest.mark.asyncio
async def test_copy_from(tmp_path: Path):
    src = tmp_path / "src.txt"
    src.write_text("data")
    dst = tmp_path / "dst.txt"
    await LocalTransport().copy_from(PurePosixPath(str(src)), dst)
    assert dst.read_text() == "data"


@pytest.mark.asyncio
async def test_healthcheck_true():
    proc = _make_proc(0)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        assert await LocalTransport().healthcheck() is True


@pytest.mark.asyncio
async def test_healthcheck_false_on_exception():
    with patch("asyncio.create_subprocess_exec", side_effect=OSError("no such file")):
        assert await LocalTransport().healthcheck() is False
