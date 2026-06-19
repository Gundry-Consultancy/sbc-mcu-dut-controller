"""M3 tests: SSH host transport (mocked asyncssh)."""

import asyncio
from pathlib import Path, PurePosixPath
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hil_controller.hosts.ssh import SSHTransport


@pytest.fixture
def ssh_transport():
    return SSHTransport(
        host="192.168.1.234",
        user="pi",
        key_path=Path("/etc/hil/keys/rpi-displays"),
        known_hosts=None,
    )


@pytest.mark.asyncio
async def test_exec_returns_result(ssh_transport):
    mock_result = MagicMock()
    mock_result.exit_status = 0
    mock_result.stdout = "hello\n"
    mock_result.stderr = ""

    mock_conn = AsyncMock()
    mock_conn.run = AsyncMock(return_value=mock_result)
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=None)

    with patch("asyncssh.connect", return_value=mock_conn):
        result = await ssh_transport.exec(["echo", "hello"])

    assert result.exit_status == 0
    assert "hello" in result.stdout


@pytest.mark.asyncio
async def test_exec_captures_stderr(ssh_transport):
    mock_result = MagicMock()
    mock_result.exit_status = 1
    mock_result.stdout = ""
    mock_result.stderr = "command not found\n"

    mock_conn = AsyncMock()
    mock_conn.run = AsyncMock(return_value=mock_result)
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=None)

    with patch("asyncssh.connect", return_value=mock_conn):
        result = await ssh_transport.exec(["bad-command"])

    assert result.exit_status == 1
    assert "not found" in result.stderr


@pytest.mark.asyncio
async def test_exec_bytes_stdin_passed_to_asyncssh_as_str(ssh_transport):
    """asyncssh runs in str/UTF-8 mode; bytes stdin must be decoded before
    conn.run(input=...), else it raises 'utf_8_encode() argument 1 must be str,
    not bytes' (regression: job 4ed00f14, dotenv secrets write over SSH)."""
    mock_result = MagicMock()
    mock_result.exit_status = 0
    mock_result.stdout = ""
    mock_result.stderr = ""

    mock_conn = AsyncMock()
    mock_conn.run = AsyncMock(return_value=mock_result)
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=None)

    with patch("asyncssh.connect", return_value=mock_conn):
        await ssh_transport.exec(["tee", "/tmp/.env"], stdin=b"K=V\n")

    sent = mock_conn.run.call_args.kwargs["input"]
    assert sent == "K=V\n"
    assert isinstance(sent, str)


@pytest.mark.asyncio
async def test_exec_tolerates_non_utf8_output(ssh_transport):
    """Remote build/serial output may contain non-UTF-8 bytes; exec must not
    raise (errors='replace')."""
    mock_result = MagicMock()
    mock_result.exit_status = 0
    mock_result.stdout = "ok�"
    mock_result.stderr = ""

    mock_conn = AsyncMock()
    mock_conn.run = AsyncMock(return_value=mock_result)
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=None)

    with patch("asyncssh.connect", return_value=mock_conn):
        result = await ssh_transport.exec(["pio", "run"])

    assert result.exit_status == 0
    assert mock_conn.run.call_args.kwargs.get("errors") == "replace"


@pytest.mark.asyncio
async def test_healthcheck_true_when_ssh_succeeds(ssh_transport):
    mock_result = MagicMock()
    mock_result.exit_status = 0
    mock_result.stdout = ""
    mock_result.stderr = ""

    mock_conn = AsyncMock()
    mock_conn.run = AsyncMock(return_value=mock_result)
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=None)

    with patch("asyncssh.connect", return_value=mock_conn):
        ok = await ssh_transport.healthcheck()
    assert ok is True


@pytest.mark.asyncio
async def test_healthcheck_false_when_ssh_fails(ssh_transport):
    with patch("asyncssh.connect", side_effect=ConnectionRefusedError("refused")):
        ok = await ssh_transport.healthcheck()
    assert ok is False
