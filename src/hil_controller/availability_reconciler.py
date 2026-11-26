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
from datetime import UTC, datetime, timedelta, timezone

from hil_controller import availability
from hil_controller.config import get_settings
from hil_controller.db.connection import get_db, now_iso

log = logging.getLogger(__name__)

# An async presence probe: "is this device actually here right now?".
# It returns either a bare bool, or ``(present, reason)`` where ``reason`` says
# why the device is absent. A reason replaces the stored ``unavailable_reason``,
# so callers see the latest finding instead of whatever first flagged the device.
ProbeOutcome = bool | tuple[bool, str | None]
Probe = Callable[[dict], Awaitable[ProbeOutcome]]


async def _default_probe(device: dict) -> bool:
    """Fallback probe for headless runs and tests — always reports absent.

    The bench probe (by-path node / bridge busid / dmesg, see
    docs/device-availability.md "Presence probe") is built in ``main.py`` from
    the host registry and passed in as ``probe=``.
    """
    return False


def _outcome(result: ProbeOutcome) -> tuple[bool, str | None]:
    """Normalise a probe result to ``(present, reason)``."""
    if isinstance(result, tuple):
        present, reason = result
        return bool(present), (reason or None)
    return bool(result), None


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
    steady_retry_s: float | None = availability.DEFAULT_STEADY_RETRY_S,
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
        attempts = device.get("retry_attempts") or 0
        decision = availability.next_retry(
            kind=device.get("unavailable_kind"),
            retry_attempts=attempts,
            retry_after=_parse_iso(device.get("retry_after")),
            now=now,
            max_attempts=max_attempts,
            steady_retry_s=steady_retry_s,
        )
        if decision.action != "retry_now":
            continue

        try:
            healed, reason = _outcome(await probe(device))
        except Exception as exc:  # a probe blowing up is a failed attempt, not a crash
            log.exception("availability probe raised for device %s", device.get("id"))
            healed, reason = False, f"presence probe error: {exc}"

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
                # Burst spacing while attempts remain, then the slow steady
                # cadence — the device keeps being rechecked on a schedule.
                if attempts + 1 >= max_attempts and steady_retry_s is not None:
                    delay = timedelta(seconds=steady_retry_s)
                else:
                    delay = availability.backoff(window_s=window_s, max_attempts=max_attempts)
                retry_after = (now + delay).isoformat()
                # A probe that says *why* the device is absent replaces the stored
                # reason (e.g. a stale "host unreachable" once the host is back but
                # the DUT is unplugged); a bare False leaves it as it was.
                await db.execute(
                    "UPDATE devices SET retry_attempts = retry_attempts + 1, "
                    "retry_after = ?, last_checked_at = ?, "
                    "unavailable_reason = COALESCE(?, unavailable_reason) WHERE id = ?",
                    (retry_after, now_iso(), reason, device["id"]),
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
        steady_retry_s: float | None = None,
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
        self.steady_retry_s: float | None = (
            steady_retry_s if steady_retry_s is not None else settings.avail_steady_retry_s
        )
        if self.steady_retry_s is not None and self.steady_retry_s <= 0:
            self.steady_retry_s = None  # 0 / negative disables steady rechecks
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
                    steady_retry_s=self.steady_retry_s,
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
