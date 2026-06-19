"""Background task that keeps host hardware fresh.

Every ``host_load_s`` (default 60s) it refreshes each transport-reachable host's
live load, and — on the same tick — re-probes a host's *static* specs whenever
they're missing or older than ``host_specs_refresh_s`` (so a board swap is
picked up automatically within a day, or immediately via the UI refresh button).

The work-speed benchmark is **never** run here — it loads the box for seconds;
operators trigger it explicitly from the UI. A host that's offline/unreachable
this tick is skipped silently (its last-known values stand) so a powered-down
on-demand DUT doesn't spam logs. See :mod:`hil_controller.host_hardware`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from hil_controller import host_hardware
from hil_controller.config import get_settings
from hil_controller.db.connection import get_db

log = logging.getLogger(__name__)


async def refresh_once(db_path: str, registry: Any, *, specs_max_age_s: int) -> None:
    """One pass: refresh load for every host, and stale/missing specs too."""
    async with get_db(db_path) as db:
        cur = await db.execute(
            "SELECT id, transport, hw_detected_json, specs_detected_at FROM hosts"
        )
        rows = [dict(r) for r in await cur.fetchall()]

    for row in rows:
        host_id = row["id"]
        if (row.get("transport") or "ssh") == "none":
            continue  # no exec transport — nothing to probe
        try:
            transport = registry.transport_for(host_id)
        except (KeyError, AttributeError):
            continue  # host not in topology / no transport — nothing to probe

        try:
            load = await host_hardware.probe_load(transport)
            await host_hardware.store_load(db_path, host_id, load)
        except Exception:  # noqa: BLE001 — an unreachable host is expected, not fatal
            log.debug("load probe failed for host %s (likely offline)", host_id)
            continue  # if load failed the host is down; don't bother with specs

        if host_hardware.specs_are_stale(row, max_age_s=specs_max_age_s):
            try:
                specs = await host_hardware.probe_specs(transport)
                await host_hardware.store_specs(db_path, host_id, specs)
                log.info("host %s specs detected: %s", host_id, specs.get("model"))
            except Exception:  # noqa: BLE001
                log.debug("spec probe failed for host %s", host_id)


class HostHardwareMonitor:
    """Periodic refresher for host load + static specs."""

    def __init__(
        self,
        db_path: str,
        registry: Any,
        *,
        interval_s: int | None = None,
        specs_max_age_s: int | None = None,
    ) -> None:
        settings = get_settings()
        self.db_path = db_path
        self.registry = registry
        self.interval_s = interval_s if interval_s is not None else settings.host_load_s
        self.specs_max_age_s = (
            specs_max_age_s if specs_max_age_s is not None else settings.host_specs_refresh_s
        )
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        while True:
            try:
                await refresh_once(
                    self.db_path, self.registry, specs_max_age_s=self.specs_max_age_s
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # never let one bad tick kill the loop
                log.exception("host hardware refresh tick failed")
            await asyncio.sleep(self.interval_s)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
