"""In-process event bus: publish job events and wake long-poll waiters."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable, Coroutine
from typing import Any

Handler = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._conditions: dict[str, asyncio.Condition] = {}

    def _condition(self, job_id: str) -> asyncio.Condition:
        if job_id not in self._conditions:
            self._conditions[job_id] = asyncio.Condition()
        return self._conditions[job_id]

    def subscribe(self, job_id: str, handler: Handler) -> None:
        self._handlers[job_id].append(handler)

    def unsubscribe(self, job_id: str, handler: Handler) -> None:
        self._handlers[job_id] = [h for h in self._handlers[job_id] if h is not handler]

    async def publish(self, job_id: str, event: dict[str, Any]) -> None:
        for handler in list(self._handlers.get(job_id, [])):
            try:
                await handler(event)
            except Exception:
                pass
        cond = self._condition(job_id)
        async with cond:
            cond.notify_all()

    async def wait_for_events(self, job_id: str, timeout: float) -> None:
        """Block until any event is published for job_id, or timeout expires."""
        cond = self._condition(job_id)
        async with cond:
            try:
                await asyncio.wait_for(cond.wait(), timeout=timeout)
            except TimeoutError:
                pass

    def cleanup(self, job_id: str) -> None:
        self._handlers.pop(job_id, None)
        self._conditions.pop(job_id, None)
