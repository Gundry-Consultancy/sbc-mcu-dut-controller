"""Automatic recovery from a wedged host USB stack (e.g. rpi-displays ``dwc_otg``).

When ``enter_bootloader`` raises :class:`~hil_controller.adapters.bench_stages.HostUsbWedgedError`
— a DUT absent from the bus that no power-cycle brings back — the ``dwc_otg`` USB
stack on the DUT host is the likely culprit. It is **NOT runtime-rebindable**, so
only a host reboot clears it (and a wedge silently hides the host's *other* DUTs
too). This module turns that signal into action:

* :func:`mark_host_wedged` — flag every device on the host ``unavailable`` /
  ``temporary`` and set the host ``reboot_required``. ``/v1/targets`` then reports
  the devices skipped, and ``RealHostRegistry.get_adapter`` refuses to start new
  jobs on them (the gate).
* :func:`recover_wedged_hosts` — for each ``reboot_required`` host, once its
  in-flight jobs **drain**, reboot it (``all_off`` → ``sudo reboot`` → wait for it
  back → ``all_on``) and clear the devices back to ``available``. **Guarded by
  ``HIL_AUTO_HOST_REBOOT`` (default off)**: when off it only logs that a manual
  reboot is required — rebooting a shared bench host is disruptive.

The live reboot is injected (``reboot_fn`` / a transport) so the policy is
unit-testable without a bench. See ``docs/device-availability.md`` and the memory
``dwc-otg-wedge-hides-bench-reboot-fixes``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from hil_controller.db.connection import get_db, now_iso

log = logging.getLogger(__name__)

#: Real dwc_otg / USB enumeration FAILURES in `dmesg` — distinct from the benign
#: "new full-speed USB device number N using dwc_otg" enumeration lines (those
#: contain "dwc_otg" too, so a bare "dwc_otg" match false-positives). A single
#: transient "error -32" that then enumerates is normal on this bench; the probe
#: treats the by-path node as the primary presence signal and these as a storm
#: hint. Patterns seen on rpi-displays: `error -110` (FSM timeout), `error -71/-32`
#: (descriptor read), `Timed out waiting for FSM`, `device not accepting address`,
#: `unable to enumerate USB device`.
USB_DMESG_ERROR_RE = re.compile(
    r"error -(110|71|32|62)\b"
    r"|Timed out waiting for FSM"
    r"|device descriptor read/\w+, error"
    r"|device not accepting address"
    r"|unable to enumerate USB device"
    r"|cannot enumerate",
    re.IGNORECASE,
)


async def _node_present(transport: Any, node: str) -> bool:
    """True if the device's by-path serial node currently exists on its host."""
    try:
        res = await asyncio.wait_for(transport.exec(["test", "-e", node]), timeout=10)
    except Exception:  # noqa: BLE001 — unreachable host / timeout = not present
        return False
    return getattr(res, "exit_status", 1) == 0


async def _await_node_present(
    transport: Any, node: str, *, timeout_s: float, poll_s: float = 1.0
) -> bool:
    """Poll until *node* enumerates, up to ``timeout_s`` — never a single-moment
    check. A freshly-powered native-USB board (e.g. an ESP32-S3) can take several
    seconds to re-enumerate after a power-on, so gating presence on one ``test -e``
    right after a fixed settle produces false "absent" verdicts. Returns True on the
    first poll that finds the node, False if the whole window elapses without it.
    """
    attempts = max(1, int(timeout_s / poll_s))
    for i in range(attempts):
        if await _node_present(transport, node):
            return True
        if i < attempts - 1:
            await asyncio.sleep(poll_s)
    return False


async def validate_presence_active(
    transport: Any,
    *,
    channel: int,
    node: str,
    settle_s: int = 25,
    turn_on_cmd: str = "~/turn_on.sh",
    turn_off_cmd: str = "~/turn_off.sh",
) -> bool:
    """**Active** presence check for the on-demand power model: power the DUT's
    channel ON, wait for it to enumerate (polled over a window, not a single check),
    confirm dmesg is clean, then power it OFF again (idle state). Returns True iff
    present + clean.

    Needed because under on-demand power an idle device is OFF — a passive
    ``test -e`` would always say "absent". ``settle_s`` is the *upper bound* of the
    appearance window (a board can take seconds to re-enumerate), not a fixed wait.
    The channel is always turned off again in a ``finally`` so a validation never
    leaves a DUT powered.
    """
    try:
        await asyncio.wait_for(
            transport.exec(["bash", "-lc", f"{turn_on_cmd} {int(channel)}"]), timeout=10
        )
    except Exception:  # noqa: BLE001 — can't power it = can't validate = not present
        return False
    try:
        if not await _await_node_present(transport, node, timeout_s=settle_s):
            return False
        return await dmesg_usb_error_count(transport) == 0
    finally:
        try:
            await asyncio.wait_for(
                transport.exec(["bash", "-lc", f"{turn_off_cmd} {int(channel)}"]), timeout=10
            )
        except Exception:  # noqa: BLE001 — best effort; leave-on is the only failure mode
            log.warning("validate_presence_active: turn_off ch %s failed", channel)


async def dmesg_usb_error_count(transport: Any, *, tail: int = 60) -> int:
    """Count recent real USB-enumeration errors in the host's dmesg tail.

    Benign "new … device … using dwc_otg" lines are NOT counted (see
    :data:`USB_DMESG_ERROR_RE`). 0 = a clean bus; >0 = a dwc_otg/USB error storm.
    """
    try:
        res = await asyncio.wait_for(
            transport.exec(["bash", "-lc", f"dmesg | tail -n {int(tail)}"]), timeout=10
        )
    except Exception:  # noqa: BLE001 — can't read dmesg = treat as unknown/clean
        return 0
    out = getattr(res, "stdout", "") or ""
    return len(USB_DMESG_ERROR_RE.findall(out))


#: Prefix of the ``unavailable_reason`` written for a wedge — also the LIKE used
#: to clear ONLY wedge-flagged devices on recovery (never a device offlined for
#: another reason).
WEDGED_REASON_PREFIX = "host USB stack wedged"

#: Prefix written when a stage fails because the DUT host is **network-unreachable**
#: (``No route to host`` / SSH connection drop) rather than USB-wedged. Unlike a
#: wedge this is NOT SSH-rebootable (the box is already off the network); it
#: self-recovers, so we only flag the devices ``temporary`` + ``retry_after`` and
#: let the reconciler's presence probe clear them once the host is back.
UNREACHABLE_REASON_PREFIX = "host unreachable"

#: Job states that mean a worker is still using the host (not yet drained).
_ACTIVE_STATES = ("assigned", "preparing", "flashing", "running")

#: A callable that actually reboots a host by id and returns True on success.
RebootFn = Callable[[str], Awaitable[bool]]


async def mark_host_wedged(
    db_path: str, host_id: str, detail: str = "", *, reboot_eta_s: int = 300
) -> str:
    """Flag every device on *host_id* unavailable/temporary + host reboot_required.

    Matches devices by ``host_id`` OR ``hub_host_id`` (the USB-server host). Idempotent.
    Returns the reason string written.

    ``reboot_eta_s`` is the expected downtime: the flagged devices get
    ``retry_after = now + reboot_eta_s`` and the reason notes "back ~Ns", so a CI
    ``wait_for_target_available`` helper sleeps until then and re-polls instead of
    hard-failing on the transient outage. A dwc_otg reboot self-heals in ~3–5 min.
    """
    reason = f"{WEDGED_REASON_PREFIX} — {host_id} reboot required, back ~{reboot_eta_s}s"
    if detail:
        reason += f" ({detail})"
    retry_after = await _flag_host_devices(db_path, host_id, reason, reboot_eta_s)
    async with get_db(db_path) as db:
        await db.execute("UPDATE hosts SET status='reboot_required' WHERE id=?", (host_id,))
        await db.commit()
    log.warning(
        "host %s flagged wedged → reboot_required, retry_after=%s (%s)",
        host_id,
        retry_after,
        reason,
    )
    return reason


async def mark_host_unreachable(
    db_path: str, host_id: str, detail: str = "", *, reboot_eta_s: int = 300
) -> str:
    """Flag *host_id*'s devices unavailable/temporary + ``retry_after`` after a
    **network-unreachable** stage failure (``No route to host`` / SSH drop).

    Same device flagging as :func:`mark_host_wedged` (so ``/v1/targets`` advertises
    the outage and the ``get_adapter`` gate refuses new jobs), but **does not** set
    the host ``reboot_required``: an off-the-network host can't be SSH-rebooted, and
    it self-recovers. The reconciler's presence probe clears the devices once the
    host is reachable again. Idempotent; returns the reason string written.
    """
    reason = f"{UNREACHABLE_REASON_PREFIX} — {host_id} unreachable, back ~{reboot_eta_s}s"
    if detail:
        reason += f" ({detail})"
    retry_after = await _flag_host_devices(db_path, host_id, reason, reboot_eta_s)
    log.warning(
        "host %s flagged unreachable (no reboot — self-recovers), retry_after=%s (%s)",
        host_id,
        retry_after,
        reason,
    )
    return reason


async def _flag_host_devices(db_path: str, host_id: str, reason: str, reboot_eta_s: int) -> str:
    """Flag every device on *host_id* (by ``host_id`` OR ``hub_host_id``)
    ``unavailable``/``temporary`` with ``retry_after = now + reboot_eta_s``.
    Returns the ISO ``retry_after`` written. Shared by the wedge/unreachable paths.
    """
    now = now_iso()
    retry_after = (datetime.now(UTC) + timedelta(seconds=reboot_eta_s)).isoformat()
    async with get_db(db_path) as db:
        # Reset retry_attempts to 0: each NEW outage episode gets a fresh self-heal
        # budget. Without this, a device that exhausted its tries (give_up) during a
        # past outage stays unavailable forever even after the host recovers — the
        # reconciler never re-probes a give_up'd device (seen live: 8 DUTs stuck at
        # attempts=3 after the passive probe kept failing on idle-off boards).
        await db.execute(
            "UPDATE devices SET status='unavailable', unavailable_kind='temporary', "
            "unavailable_reason=?, unavailable_since=COALESCE(unavailable_since, ?), "
            "retry_after=?, retry_attempts=0, last_checked_at=? WHERE host_id=? OR hub_host_id=?",
            (reason, now, retry_after, now, host_id, host_id),
        )
        await db.commit()
    return retry_after


async def host_active_job_count(db_path: str, host_id: str) -> int:
    """How many jobs are still assigned to *host_id* in a non-terminal state."""
    placeholders = ",".join("?" * len(_ACTIVE_STATES))
    async with get_db(db_path) as db:
        cur = await db.execute(
            f"SELECT COUNT(*) AS n FROM jobs WHERE assigned_host=? AND state IN ({placeholders})",
            (host_id, *_ACTIVE_STATES),
        )
        row = await cur.fetchone()
    return int(row["n"]) if row else 0


async def list_reboot_required_hosts(db_path: str) -> list[str]:
    async with get_db(db_path) as db:
        cur = await db.execute("SELECT id FROM hosts WHERE status='reboot_required'")
        rows = await cur.fetchall()
    return [r["id"] for r in rows]


async def clear_host_recovered(db_path: str, host_id: str) -> None:
    """Clear ONLY the wedge-flagged devices on *host_id* back to available + host up."""
    now = now_iso()
    async with get_db(db_path) as db:
        await db.execute(
            "UPDATE devices SET status='available', unavailable_kind=NULL, "
            "unavailable_reason=NULL, unavailable_since=NULL, retry_attempts=0, "
            "retry_after=NULL, last_checked_at=? "
            "WHERE (host_id=? OR hub_host_id=?) AND unavailable_reason LIKE ?",
            (now, host_id, host_id, WEDGED_REASON_PREFIX + "%"),
        )
        await db.execute(
            "UPDATE hosts SET status='available', last_seen_at=? WHERE id=?", (now, host_id)
        )
        await db.commit()
    log.warning("host %s recovered — wedge-flagged devices cleared to available", host_id)


async def reboot_host(
    transport: Any,
    *,
    channel_nodes: list[tuple[int, str]] | None = None,
    all_off_cmd: str = "~/all_off.sh",
    all_on_cmd: str = "~/all_on.sh",
    turn_on_cmd: str = "~/turn_on.sh",
    turn_off_cmd: str = "~/turn_off.sh",
    wait_back_s: int = 240,
    poll_s: int = 5,
    channel_settle_s: int = 20,  # upper bound of the per-channel appearance window (polled)
) -> bool:
    """Cold-reboot a DUT host: all_off → ``sudo reboot`` → wait for it back → re-power.

    Re-powering is REQUIRED afterwards (a fresh boot leaves the solenoid ports off):

    * With ``channel_nodes`` (``[(channel, by_path_node), …]``) — the **gentle
      sequential bring-up**: turn each channel on **one at a time** with a settle,
      presence-check its node + scan dmesg for a USB error storm, then turn it
      **off again** (on-demand model: DUTs idle off; a job powers its own channel
      at start). This avoids the simultaneous-enumeration ``dwc_otg`` storm that
      ``all_on`` triggers (which can leave a board — e.g. the QT Py —
      un-enumerated). Returns the per-channel presence map so the caller can mark
      validated-present devices available and still-absent ones temporary.
    * Without it — legacy ``all_on`` (used when no channel map is available).

    Returns True iff the host came back and re-powered. ``transport`` must expose an
    async ``exec(argv)`` returning an object with ``exit_status`` (and ``stdout``).
    """

    async def _exec(argv: list[str]) -> Any:
        return await transport.exec(argv)

    log.warning("host reboot: powering all ports off")
    try:
        await _exec(["bash", "-lc", all_off_cmd])
    except Exception as exc:  # noqa: BLE001 — best effort; reboot is what matters
        log.warning("all_off before reboot failed (continuing): %s", exc)

    log.warning("host reboot: issuing sudo reboot (connection will drop)")
    try:
        await _exec(["sudo", "reboot"])
    except Exception:  # noqa: BLE001 — the box is going down; the drop is expected
        pass

    back = False
    attempts = max(1, wait_back_s // poll_s)
    for _ in range(attempts):
        await asyncio.sleep(poll_s)
        try:
            res = await _exec(["true"])
            if getattr(res, "exit_status", 1) == 0:
                back = True
                break
        except Exception:  # noqa: BLE001 — still down; keep waiting
            continue
    if not back:
        log.error("host reboot: host did not come back within %ds", wait_back_s)
        return False

    if not channel_nodes:
        log.warning("host reboot: back up; powering all ports on (legacy all_on)")
        try:
            await _exec(["bash", "-lc", all_on_cmd])
        except Exception as exc:  # noqa: BLE001
            log.error("all_on after reboot failed: %s", exc)
            return False
        return True

    log.warning(
        "host reboot: back up; sequential per-channel bring-up (%d channels)", len(channel_nodes)
    )
    for ch, node in channel_nodes:
        try:
            await _exec(["bash", "-lc", f"{turn_on_cmd} {int(ch)}"])
        except Exception as exc:  # noqa: BLE001 — keep going; report what we can
            log.warning("recovery: turn_on ch %s failed: %s", ch, exc)
            continue
        # Poll for the node over a window (a board can take seconds to enumerate),
        # not a single check after a fixed settle.
        present = (
            await _await_node_present(transport, node, timeout_s=channel_settle_s) if node else None
        )
        errs = await dmesg_usb_error_count(transport)
        log.warning("recovery ch %s: present=%s dmesg_usb_errors=%s", ch, present, errs)
        # On-demand: leave the channel OFF again; a job powers its own channel.
        try:
            await _exec(["bash", "-lc", f"{turn_off_cmd} {int(ch)}"])
        except Exception as exc:  # noqa: BLE001
            log.warning("recovery: turn_off ch %s failed: %s", ch, exc)
    return True


async def recover_wedged_hosts(
    db_path: str,
    *,
    reboot_fn: RebootFn,
    enabled: bool,
) -> list[str]:
    """For each ``reboot_required`` host: drain → (if enabled) reboot → clear.

    Returns the list of host ids actually recovered this pass. When *enabled* is
    False, logs loudly that a manual reboot is required and recovers nothing.
    """
    recovered: list[str] = []
    for host_id in await list_reboot_required_hosts(db_path):
        active = await host_active_job_count(db_path, host_id)
        if active > 0:
            log.info(
                "host %s reboot deferred — %d job(s) in flight (draining first)",
                host_id,
                active,
            )
            continue
        if not enabled:
            log.warning(
                "host %s needs a reboot to clear a wedged USB stack, but "
                "HIL_AUTO_HOST_REBOOT is off — reboot it manually, then clear it",
                host_id,
            )
            continue
        log.warning("host %s: auto-rebooting to clear wedged USB stack", host_id)
        ok = False
        try:
            ok = await reboot_fn(host_id)
        except Exception:  # noqa: BLE001
            log.exception("auto host reboot failed for %s", host_id)
        if ok:
            await clear_host_recovered(db_path, host_id)
            recovered.append(host_id)
        else:
            log.error("host %s reboot did not complete; left flagged for next pass", host_id)
    return recovered
