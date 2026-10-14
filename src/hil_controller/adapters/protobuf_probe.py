"""Assert actual I2C readings after generic protobuf Add/Modify injection."""

from __future__ import annotations

import asyncio
import math
from typing import Any

from hil_controller.adapters.ws_i2c_inject import WsI2cProbeInjector, parse_i2c_event


def check_i2c_samples(samples: list[dict[str, Any]], expected: dict[str, Any]) -> dict[str, Any]:
    minimum = int(expected.get("min_samples", 3))
    if minimum < 1 or len(samples) < minimum:
        raise ValueError(f"need {minimum} matching I2C events, got {len(samples)}")
    selected = samples[-minimum:]
    reference = expected.get("sea_level_pressure")
    for sample in selected:
        values = sample["readings"]
        for kind, limits in expected.get("readings", {}).items():
            value = values.get(int(kind))
            if value is None or not math.isfinite(value):
                raise ValueError(f"missing/nonfinite sensor type {kind}")
            if not limits.get("min", -math.inf) <= value <= limits.get("max", math.inf):
                raise ValueError(f"sensor type {kind} outside expected range: {value}")
        if reference is not None:
            pressure, altitude = values.get(6), values.get(27)
            if (
                pressure is None
                or altitude is None
                or not math.isfinite(pressure)
                or not math.isfinite(reference)
                or pressure <= 0
                or reference <= 0
            ):
                raise ValueError("pressure and altitude required to verify sea_level_pressure")
            predicted = 44330.0 * (1.0 - (pressure / reference) ** 0.1903)
            if not math.isfinite(altitude) or abs(altitude - predicted) > 2.0:
                raise ValueError(
                    f"sea_level_pressure not applied: altitude={altitude}, expected={predicted}"
                )
    return {
        "ok": True,
        "samples": len(samples),
        "checked": selected,
        "sea_level_pressure": reference,
    }


async def publish_and_check_i2c(
    injector: WsI2cProbeInjector,
    topic: str,
    payload: bytes,
    expected: dict[str, Any],
    *,
    observe_s: float = 12,
) -> dict[str, Any]:
    import aiomqtt

    samples: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    async with aiomqtt.Client(hostname=injector.broker_host, port=injector.mqtt_port) as client:
        await client.subscribe(topic.replace("/ws-b2d/", "/ws-d2b/"))
        await injector._echo(topic, payload)
        started = loop.time()
        messages = aiter(client.messages)
        while loop.time() < started + observe_s and len(samples) < 256:
            try:
                message = await asyncio.wait_for(anext(messages), started + observe_s - loop.time())
            except (TimeoutError, StopAsyncIteration):
                break
            # Ignore queued samples from the old component during Modify settling.
            if loop.time() - started < 1.0 or not isinstance(message.payload, bytes):
                continue
            try:
                event = parse_i2c_event(message.payload)
            except (ValueError, IndexError):
                continue
            if (
                event
                and event["address"] == expected["address"]
                and (event.get("mux_channel") or 0) == expected.get("mux_channel", 0)
            ):
                samples.append(event)
    return check_i2c_samples(samples, expected)
