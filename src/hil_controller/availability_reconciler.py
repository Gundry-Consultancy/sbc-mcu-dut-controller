"""Async reconciler that self-heals temporary device outages.

See docs/device-availability.md. This is the thin async timer wrapper around the
pure :mod:`hil_controller.availability` policy: every ``HIL_AVAIL_RECONCILE_S``
seconds it loads ``temporary``-unavailable devices and, for each one the policy
says is due (``next_retry(...).action == "retry_now"``), runs a presence probe.

* probe success → ``status='available'`` and the availability columns cleared.
* probe failure → ``retry_attempts += 1``, ``retry_after = now + backoff()``,
  ``last_checked_at = now``.

``permanent`` outages are never loaded/touched (the SQL filters them out and the
policy returns ``not_applicable`` for them anyway). The presence probe is an
injectable async callable so the logic is testable without a bench.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timezone

from hil_controller import availability
from hil_controller.config import get_settings
from hil_controller.db.connection import get_db, now_iso

log = logging.getLogger(__name__)

# An async presence probe: "is this device actually here right now?".
Probe = Callable[[dict], Awaitable[bool]]


async def _default_probe(device: dict) -> bool:
    """Stub presence probe — always reports absent.

    TODO: the real probe checks whether the device's ``serial_port`` /
    ``hub_port_path`` enumerates on its host (e.g. the ``/dev/serial/by-path``
    node exists), per docs/device-availability.md "Presence probe". Wired to the
    bench transport in a later increment.
    """
    return False


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


async def reconcile_once(
    db_path: str,
    *,
    probe: Probe,
    max_attempts: int,
    window_s: int,
    now: datetime | None = None,
) -> None:
    """Run a single reconcile pass over temporary-unavailable devices."""
    now = now or datetime.now(UTC)
    async with get_db(db_path) as db:
        cur = await db.execute(
            "SELECT * FROM devices WHERE status = ? AND unavailable_kind = ?",
            (availability.STATUS_UNAVAILABLE, availability.TEMPORARY),
        )
        rows = [dict(r) for r in await cur.fetchall()]

    for device in rows:
        decision = availability.next_retry(
            kind=device.get("unavailable_kind"),
            retry_attempts=device.get("retry_attempts") or 0,
            retry_after=_parse_iso(device.get("retry_after")),
            now=now,
            max_attempts=max_attempts,
        )
        if decision.action != "retry_now":
            continue

        try:
            healed = await probe(device)
        except Exception:  # a probe blowing up is a failed attempt, not a crash
            log.exception("availability probe raised for device %s", device.get("id"))
            healed = False

        async with get_db(db_path) as db:
            if healed:
                await db.execute(
                    "UPDATE devices SET status = ?, unavailable_kind = NULL, "
                    "unavailable_reason = NULL, unavailable_since = NULL, "
                    "retry_attempts = 0, retry_after = NULL, last_checked_at = ? "
                    "WHERE id = ?",
                    (availability.STATUS_AVAILABLE, now_iso(), device["id"]),
                )
                log.info("device %s healed (presence probe ok)", device["id"])
            else:
                retry_after = (
                    now + availability.backoff(window_s=window_s, max_attempts=max_attempts)
                ).isoformat()
                await db.execute(
                    "UPDATE devices SET retry_attempts = retry_attempts + 1, "
                    "retry_after = ?, last_checked_at = ? WHERE id = ?",
                    (retry_after, now_iso(), device["id"]),
                )
            await db.commit()


class AvailabilityReconciler:
    """Background task that periodically self-heals temporary outages."""

    def __init__(
        self,
        db_path: str,
        *,
        probe: Probe | None = None,
        interval_s: int | None = None,
        max_attempts: int | None = None,
        window_s: int | None = None,
        host_reboot_fn: Callable[[str], Awaitable[bool]] | None = None,
        auto_host_reboot: bool | None = None,
    ) -> None:
        settings = get_settings()
        self.db_path = db_path
        self.probe: Probe = probe or _default_probe
        self.interval_s = interval_s if interval_s is not None else settings.avail_reconcile_s
        self.max_attempts = (
            max_attempts if max_attempts is not None else settings.avail_retry_attempts
        )
        self.window_s = window_s if window_s is not None else settings.avail_retry_window_s
        # Host-USB-wedge recovery (dwc_otg). With a reboot fn supplied, each tick
        # also reboots any reboot_required host whose jobs have drained — but only
        # if auto_host_reboot is on (else it just logs that a manual reboot is due).
        self.host_reboot_fn = host_reboot_fn
        self.auto_host_reboot = (
            auto_host_reboot if auto_host_reboot is not None else settings.auto_host_reboot
        )
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        while True:
            try:
                await reconcile_once(
                    self.db_path,
                    probe=self.probe,
                    max_attempts=self.max_attempts,
                    window_s=self.window_s,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # never let one bad tick kill the loop
                log.exception("availability reconcile tick failed")
            if self.host_reboot_fn is not None:
                try:
                    from hil_controller.host_recovery import recover_wedged_hosts

                    await recover_wedged_hosts(
                        self.db_path,
                        reboot_fn=self.host_reboot_fn,
                        enabled=self.auto_host_reboot,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:  # a bad host-recovery tick must not kill the loop
                    log.exception("host-wedge recovery tick failed")
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
