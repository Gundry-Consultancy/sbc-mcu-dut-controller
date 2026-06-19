"""ProtoMQ observer: activate a script on the broker, stream MQTT traffic as job log events."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

import httpx

log = logging.getLogger(__name__)

try:
    import aiomqtt

    _AIOMQTT_AVAILABLE = True
except ImportError:
    _AIOMQTT_AVAILABLE = False

EmitFn = Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]]


class ProtoMQObserver:
    """
    Drives the ProtoMQ broker before/during/after a job's run phase.

    Lifecycle:
      1. activate_script(name)  — POST /api/scripts/{name}/activate
      2. observe(emit_log)      — subscribe MQTT #, forward messages as log events (run as Task)
      3. get_script_status()    — GET /api/scripts → completed steps
      4. deactivate()           — POST /api/scripts/deactivate
    """

    def __init__(
        self,
        broker_host: str,
        mqtt_port: int = 1884,
        api_url: str | None = None,
    ) -> None:
        self.broker_host = broker_host
        self.mqtt_port = mqtt_port
        self.api_url = api_url or f"http://{broker_host}:5173"

    # ---------------------------------------------------------------------- #
    # HTTP API calls                                                           #
    # ---------------------------------------------------------------------- #

    async def activate_script(self, name: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{self.api_url}/api/scripts/{name}/activate")
            r.raise_for_status()
            return r.json()

    async def deactivate(self) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{self.api_url}/api/scripts/deactivate")
            r.raise_for_status()

    async def get_script_status(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{self.api_url}/api/scripts")
            r.raise_for_status()
            data = r.json()
        active = next((s for s in data.get("scripts", []) if s.get("active")), None)
        return {
            "active_script": active.get("filename") if active else None,
            "completed_steps": active.get("completedSteps", []) if active else [],
        }

    # ---------------------------------------------------------------------- #
    # MQTT observation                                                          #
    # ---------------------------------------------------------------------- #

    async def observe(self, emit_log: EmitFn) -> None:
        if not _AIOMQTT_AVAILABLE:
            await emit_log(
                "log",
                {"stream": "protomq", "msg": "aiomqtt not installed; MQTT observation disabled"},
            )
            return
        try:
            async with aiomqtt.Client(hostname=self.broker_host, port=self.mqtt_port) as client:
                await client.subscribe("#")
                log.debug(
                    "ProtoMQ observer subscribed to # on %s:%d", self.broker_host, self.mqtt_port
                )
                async for message in client.messages:
                    await self._handle(message, emit_log)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("ProtoMQ observe error: %s", exc)
            await emit_log("log", {"stream": "protomq", "msg": f"observer error: {exc}"})

    async def _handle(self, message: Any, emit_log: EmitFn) -> None:
        topic = str(message.topic)
        payload = message.payload

        if isinstance(payload, bytes):
            is_protobuf = "/ws-d2b/" in topic or "/ws-b2d/" in topic
            if is_protobuf:
                msg_text = f"<protobuf {len(payload)}b>"
            else:
                msg_text = _safe_decode(payload)
        else:
            msg_text = str(payload)[:500]

        await emit_log(
            "log",
            {
                "stream": "protomq",
                "topic": topic,
                "msg": f"[{topic}] {msg_text}",
            },
        )


def _safe_decode(data: bytes, limit: int = 500) -> str:
    try:
        return data.decode("utf-8")[:limit]
    except UnicodeDecodeError:
        return f"<binary {len(data)}b>"
