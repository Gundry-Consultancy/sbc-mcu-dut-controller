"""Tests for ShellScriptAdapter: inline script execution via transport."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from hil_controller.adapters.shell_script import ShellScriptAdapter
from hil_controller.hosts.base import ExecResult


def _transport(exit_status=0, stdout="hello\n", stderr=""):
    t = MagicMock()
    t.exec = AsyncMock(
        return_value=ExecResult(exit_status=exit_status, stdout=stdout, stderr=stderr)
    )
    return t


@pytest.mark.asyncio
async def test_run_pass_on_zero_exit():
    t = _transport(exit_status=0, stdout="ok\n")
    adapter = ShellScriptAdapter(transport=t, script="echo ok")
    result = await adapter.run()
    assert result == "pass"
    assert adapter._run_stdout == "ok\n"
    t.exec.assert_awaited_once_with(["sh", "-c", "echo ok"])


@pytest.mark.asyncio
async def test_run_fail_on_nonzero_exit():
    t = _transport(exit_status=1, stderr="oops")
    adapter = ShellScriptAdapter(transport=t, script="false")
    result = await adapter.run()
    assert result == "fail"
    assert adapter._run_stderr == "oops"


@pytest.mark.asyncio
async def test_acquire_and_release_are_noops():
    adapter = ShellScriptAdapter(transport=MagicMock(), script="true")
    await adapter.acquire()
    await adapter.release()
