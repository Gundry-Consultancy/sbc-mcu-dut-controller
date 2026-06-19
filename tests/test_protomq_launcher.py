"""Tests for the per-session protomq launcher (no protomq edits)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from hil_controller.adapters.protomq_launcher import (
    BUILD_COMMAND,
    ProtomqLauncher,
    ProtomqLaunchError,
    clone_argv,
    parse_listen_ports,
    start_argv,
)
from hil_controller.hosts.base import ExecResult


def _result(exit_status: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    r = MagicMock(spec=ExecResult)
    r.exit_status = exit_status
    r.stdout = stdout
    r.stderr = stderr
    return r


# --------------------------------------------------------------------------- #
# parse_listen_ports                                                          #
# --------------------------------------------------------------------------- #

_BOOT = """\
MQTT listening on port 1884
MQTT-via-WebSocket listening on port 8888
HTTP frontend & API listening on port 5173
HTTP frontend url: http://localhost:5173
"""


def test_parse_all_three_ports() -> None:
    got = parse_listen_ports(_BOOT)
    assert got == {"mqtt": 1884, "ws": 8888, "api": 5173}


def test_parse_mqtt_does_not_capture_websocket_line() -> None:
    # The MQTT-via-WebSocket line must register as ws, never as mqtt.
    got = parse_listen_ports("MQTT-via-WebSocket listening on port 8889")
    assert got == {"ws": 8889}


def test_parse_incremented_ports_are_honoured() -> None:
    # Whatever protomq prints is what we use — including bumped ports.
    got = parse_listen_ports(
        "MQTT listening on port 1886\nHTTP frontend & API listening on port 5175"
    )
    assert got == {"mqtt": 1886, "api": 5175}


def test_parse_empty() -> None:
    assert parse_listen_ports("") == {}
    assert parse_listen_ports("nothing here") == {}


# --------------------------------------------------------------------------- #
# argv builders                                                               #
# --------------------------------------------------------------------------- #


def test_clone_argv() -> None:
    assert clone_argv("https://x/protomq.git", "displays-v2", "/tmp/p") == [
        "git",
        "clone",
        "--depth",
        "1",
        "--branch",
        "displays-v2",
        "https://x/protomq.git",
        "/tmp/p",
    ]


def test_clone_argv_embeds_pat() -> None:
    argv = clone_argv("https://github.com/o/r.git", "api-v2", "/tmp/p", pat="ghp_secret")
    assert argv == [
        "git",
        "clone",
        "--depth",
        "1",
        "--branch",
        "api-v2",
        "https://ghp_secret@github.com/o/r.git",
        "/tmp/p",
    ]


def test_clone_argv_uses_credential_helper_when_no_pat() -> None:
    argv = clone_argv(
        "https://github.com/o/r.git",
        "api-v2",
        "/tmp/p",
        credential_helper="!sudo gh auth git-credential",
    )
    assert argv[:3] == ["git", "-c", "credential.helper=!sudo gh auth git-credential"]
    assert argv[-2:] == ["https://github.com/o/r.git", "/tmp/p"]


def test_clone_argv_pat_beats_helper() -> None:
    argv = clone_argv(
        "https://github.com/o/r.git",
        "api-v2",
        "/tmp/p",
        pat="ghp_x",
        credential_helper="!sudo gh auth git-credential",
    )
    assert "-c" not in argv  # PAT path doesn't add the helper
    assert "https://ghp_x@github.com/o/r.git" in argv


def test_start_argv_plain_and_with_active_script() -> None:
    assert start_argv() == ["npm", "start"]
    assert start_argv("My Script") == ["npm", "start", "--", "--active-script=My Script"]


# --------------------------------------------------------------------------- #
# clone_and_build                                                             #
# --------------------------------------------------------------------------- #


def _launcher(transport, **kw):
    return ProtomqLauncher(
        controller_transport=transport,
        repo="https://github.com/adafruit/protomq.git",
        ref="displays-v2-testing",
        work_dir="/tmp/hil/job-1/protomq",
        **kw,
    )


@pytest.mark.asyncio
async def test_clone_and_build_clones_protomq_then_proto_sibling_then_builds() -> None:
    tp = AsyncMock()
    tp.exec = AsyncMock(return_value=_result(0))
    await _launcher(tp).clone_and_build()
    argvs = [c.args[0] for c in tp.exec.call_args_list]
    # 1) protomq clone, 2) Wippersnapper_Protobuf sibling clone, 3) build
    assert argvs[0][:3] == ["git", "clone", "--depth"]
    assert argvs[0][-1] == "/tmp/hil/job-1/protomq"
    assert "Wippersnapper_Protobuf.git" in argvs[1][-2]
    assert argvs[1][-1] == "/tmp/hil/job-1/Wippersnapper_Protobuf"  # sibling of protomq
    assert "--branch" in argvs[1] and "api-v2" in argvs[1]
    assert argvs[2] == ["bash", "-c", BUILD_COMMAND]
    assert tp.exec.call_args_list[2].kwargs["cwd"] == "/tmp/hil/job-1/protomq"


@pytest.mark.asyncio
async def test_clone_failure_raises() -> None:
    tp = AsyncMock()
    tp.exec = AsyncMock(return_value=_result(128, stderr="repository not found"))
    with pytest.raises(ProtomqLaunchError, match="clone failed"):
        await _launcher(tp).clone_and_build()


# --------------------------------------------------------------------------- #
# port discovery from a line stream                                           #
# --------------------------------------------------------------------------- #


async def _lines(items: list[str]) -> AsyncIterator[str]:
    for it in items:
        yield it


@pytest.mark.asyncio
async def test_consume_until_ready_records_ports_and_forwards() -> None:
    seen: list[str] = []
    launcher = _launcher(AsyncMock(), on_line=seen.append)
    await launcher._consume_until_ready(
        _lines(
            [
                "booting...",
                "MQTT listening on port 1884",
                "MQTT-via-WebSocket listening on port 8888",
                "HTTP frontend & API listening on port 5173",
            ]
        ),
        ready_timeout=2.0,
    )
    assert launcher.mqtt_port == 1884
    assert launcher.api_port == 5173
    assert launcher.ws_port == 8888
    assert "booting..." in seen


@pytest.mark.asyncio
async def test_consume_until_ready_stops_once_mqtt_and_api_known() -> None:
    launcher = _launcher(AsyncMock())
    # ws never announced; readiness only needs mqtt + api
    await launcher._consume_until_ready(
        _lines(["MQTT listening on port 1884", "HTTP frontend & API listening on port 5173"]),
        ready_timeout=2.0,
    )
    assert launcher.mqtt_port == 1884 and launcher.api_port == 5173


@pytest.mark.asyncio
async def test_consume_until_ready_times_out_without_ports() -> None:
    import asyncio

    async def _stuck() -> AsyncIterator[str]:
        yield "starting..."
        await asyncio.sleep(10)
        yield "never"

    launcher = _launcher(AsyncMock())
    with pytest.raises(ProtomqLaunchError, match="did not announce"):
        await launcher._consume_until_ready(_stuck(), ready_timeout=0.1)
