"""Tests for ProtoMQObserver (M5): HTTP script activation + MQTT log forwarding."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import respx
from httpx import Response

from hil_controller.adapters.protomq_observer import ProtoMQObserver, _safe_decode

API = "http://pi5-proto:5173"


def _obs() -> ProtoMQObserver:
    return ProtoMQObserver("pi5-proto", api_url=API)


# --------------------------------------------------------------------------- #
# HTTP API                                                                     #
# --------------------------------------------------------------------------- #


@respx.mock
@pytest.mark.asyncio
async def test_activate_script_posts_correct_url():
    respx.post(f"{API}/api/scripts/my-demo/activate").respond(
        200, json={"status": "OK", "active": "my-demo"}
    )
    result = await _obs().activate_script("my-demo")
    assert result["status"] == "OK"


@respx.mock
@pytest.mark.asyncio
async def test_activate_script_raises_on_404():
    respx.post(f"{API}/api/scripts/bad-name/activate").respond(404, json={"error": "not found"})
    with pytest.raises(Exception):
        await _obs().activate_script("bad-name")


@respx.mock
@pytest.mark.asyncio
async def test_deactivate_posts_correct_url():
    route = respx.post(f"{API}/api/scripts/deactivate").respond(200, json={"status": "OK"})
    await _obs().deactivate()
    assert route.called


@respx.mock
@pytest.mark.asyncio
async def test_get_status_returns_active_script_and_steps():
    respx.get(f"{API}/api/scripts").respond(
        200,
        json={
            "scripts": [
                {
                    "filename": "my-demo",
                    "active": True,
                    "completedSteps": ["checkin-response", "add-display"],
                },
                {"filename": "other", "active": False, "completedSteps": []},
            ]
        },
    )
    status = await _obs().get_script_status()
    assert status["active_script"] == "my-demo"
    assert status["completed_steps"] == ["checkin-response", "add-display"]


@respx.mock
@pytest.mark.asyncio
async def test_get_status_no_active_script():
    respx.get(f"{API}/api/scripts").respond(
        200, json={"scripts": [{"filename": "my-demo", "active": False, "completedSteps": []}]}
    )
    status = await _obs().get_script_status()
    assert status["active_script"] is None
    assert status["completed_steps"] == []


# --------------------------------------------------------------------------- #
# Message handling                                                              #
# --------------------------------------------------------------------------- #


def _fake_msg(topic: str, payload: bytes) -> MagicMock:
    msg = MagicMock()
    msg.topic.__str__ = lambda _: topic
    msg.payload = payload
    return msg


@pytest.mark.asyncio
async def test_handle_non_protobuf_topic_emits_text():
    log_events: list[tuple[str, dict]] = []

    async def emit(kind: str, payload: dict) -> None:
        log_events.append((kind, payload))

    msg = _fake_msg("state/clients", b'{"clients":[]}')
    await _obs()._handle(msg, emit)

    assert log_events[0][0] == "log"
    assert "state/clients" in log_events[0][1]["msg"]
    assert '{"clients":[]}' in log_events[0][1]["msg"]


@pytest.mark.asyncio
async def test_handle_d2b_protobuf_shows_size_not_content():
    log_events: list[tuple[str, dict]] = []

    async def emit(kind: str, payload: dict) -> None:
        log_events.append((kind, payload))

    msg = _fake_msg("user/ws-d2b/device123", bytes(range(40)))
    await _obs()._handle(msg, emit)

    assert "protobuf" in log_events[0][1]["msg"]
    assert "40" in log_events[0][1]["msg"]
    assert log_events[0][1]["topic"] == "user/ws-d2b/device123"


@pytest.mark.asyncio
async def test_handle_b2d_protobuf_shows_size_not_content():
    log_events: list[tuple[str, dict]] = []

    async def emit(kind: str, payload: dict) -> None:
        log_events.append((kind, payload))

    msg = _fake_msg("user/ws-b2d/device123", bytes(range(67)))
    await _obs()._handle(msg, emit)

    assert "protobuf" in log_events[0][1]["msg"]
    assert "67" in log_events[0][1]["msg"]


# --------------------------------------------------------------------------- #
# MQTT observe                                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_observe_emits_messages_from_mqtt():
    log_events: list[tuple[str, dict]] = []

    async def emit(kind: str, payload: dict) -> None:
        log_events.append((kind, payload))

    msg = _fake_msg("state/clients", b'{"clients":[]}')

    async def _fake_messages():
        yield msg

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.subscribe = AsyncMock()
    mock_client.messages = _fake_messages()

    with (
        patch("hil_controller.adapters.protomq_observer._AIOMQTT_AVAILABLE", True),
        patch("aiomqtt.Client", return_value=mock_client),
    ):
        await _obs().observe(emit)

    assert any(e[1].get("stream") == "protomq" for e in log_events)


@pytest.mark.asyncio
async def test_observe_skips_gracefully_without_aiomqtt():
    log_events: list[tuple[str, dict]] = []

    async def emit(kind: str, payload: dict) -> None:
        log_events.append((kind, payload))

    with patch("hil_controller.adapters.protomq_observer._AIOMQTT_AVAILABLE", False):
        await _obs().observe(emit)

    assert any("disabled" in e[1].get("msg", "") for e in log_events)


@pytest.mark.asyncio
async def test_observe_propagates_cancellation():
    async def emit(kind: str, payload: dict) -> None:
        pass

    async def _hanging_messages():
        await asyncio.sleep(9999)
        yield MagicMock()  # makes this an async generator; never reached

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.subscribe = AsyncMock()
    mock_client.messages = _hanging_messages()

    with (
        patch("hil_controller.adapters.protomq_observer._AIOMQTT_AVAILABLE", True),
        patch("aiomqtt.Client", return_value=mock_client),
    ):
        task = asyncio.create_task(_obs().observe(emit))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# --------------------------------------------------------------------------- #
# Helpers                                                                       #
# --------------------------------------------------------------------------- #


def test_safe_decode_valid_utf8():
    assert _safe_decode(b"hello") == "hello"


def test_safe_decode_binary_returns_description():
    result = _safe_decode(bytes(range(256)))
    assert "binary" in result
    assert "256" in result


def test_safe_decode_respects_limit():
    assert len(_safe_decode(b"x" * 1000, limit=10)) == 10
