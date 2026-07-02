"""Device availability policy (pure) — see docs/device-availability.md.

The DB holds the availability state on each device; this module holds the
*decisions* about it, free of I/O so they can be unit-tested without a bench:

* classify an outage as ``temporary`` (self-heal-eligible) or ``permanent``,
* decide, for a temporary outage, whether to attempt rectification now / wait /
  give up (the ≤3-tries-over-~3-min budget), and the backoff between tries,
* render a device row into the ``GET /v1/targets`` availability record.

The async reconciler (next increment) is a thin timer that calls ``next_retry``
and runs the presence probe; it owns no policy of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

TEMPORARY = "temporary"
PERMANENT = "permanent"

STATUS_AVAILABLE = "available"
STATUS_UNAVAILABLE = "unavailable"

# Defaults (overridable via env at the call site — see docs).
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_WINDOW_S = 180
# After the initial burst, keep re-probing on this slow steady cadence instead
# of giving up forever (flags used to freeze until a human edited the DB).
DEFAULT_STEADY_RETRY_S = 900


@dataclass(frozen=True)
class RetryDecision:
    """Outcome of :func:`next_retry`.

    ``action`` is one of:
      * ``"retry_now"``      — run the presence probe now.
      * ``"wait"``           — too soon; ``wait_until`` is when to look again.
      * ``"give_up"``        — burst budget exhausted AND steady rechecks are
                               disabled (``steady_retry_s=None``); stop retrying.
      * ``"not_applicable"`` — device is available or permanently unavailable.
    """

    action: str
    wait_until: datetime | None = None
    attempts_remaining: int = 0


def is_self_healable(kind: str | None) -> bool:
    """True only for ``temporary`` outages (``permanent`` is never retried)."""
    return kind == TEMPORARY


def backoff(
    window_s: float = DEFAULT_RETRY_WINDOW_S, max_attempts: int = DEFAULT_RETRY_ATTEMPTS
) -> timedelta:
    """Even spacing between attempts so ``max_attempts`` fit in ``window_s``.

    e.g. 3 attempts across 180s → a ~60s gap between tries.
    """
    step = window_s / max(1, max_attempts)
    return timedelta(seconds=step)


def next_retry(
    *,
    kind: str | None,
    retry_attempts: int,
    retry_after: datetime | None,
    now: datetime,
    max_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    steady_retry_s: float | None = DEFAULT_STEADY_RETRY_S,
) -> RetryDecision:
    """Decide whether to self-rectify a temporary outage at ``now``.

    Permanent / available devices are ``not_applicable``. A temporary device
    gets a fast burst of ``max_attempts`` tries (spaced by :func:`backoff`),
    then drops to a slow **steady cadence** (every ``steady_retry_s``) forever —
    an unavailable device keeps being rechecked on a schedule, not only right
    after the failure. Each attempt waits until ``retry_after`` first. Pass
    ``steady_retry_s=None`` to restore the old give-up-after-burst behaviour.
    """
    if not is_self_healable(kind):
        return RetryDecision(action="not_applicable")
    remaining = max_attempts - retry_attempts
    if remaining <= 0 and steady_retry_s is None:
        return RetryDecision(action="give_up", attempts_remaining=0)
    remaining = max(0, remaining)
    if retry_after is not None and now < retry_after:
        return RetryDecision(action="wait", wait_until=retry_after, attempts_remaining=remaining)
    return RetryDecision(action="retry_now", attempts_remaining=remaining)


def target_record(row: dict, *, target_key: str = "model", host_hw: dict | None = None) -> dict:
    """Render a device DB row into a ``GET /v1/targets`` availability record.

    Tolerant of rows that predate the availability columns: a missing/empty
    ``status`` (or ``status == 'available'``) reads as available. ``target`` is
    the **build-job target name** — the ``build_target`` tag (the arduino-cli
    platform name, e.g. ``qtpy_esp32s3_n4r2``) so a CI matrix can map 1:1 to the
    artifact it built; it falls back to ``row[target_key]`` (the device model)
    when no build_target is set.

    ``host_hw`` (when given) is the device's host's merged hardware view
    (:func:`hil_controller.host_hardware.host_hw_view`) — the real board model,
    CPU/RAM, live load and work-speed score — surfaced under a ``host`` key so a
    scheduler can finally tell a Pi Zero W apart from a Pi 5 instead of seeing
    every SBC report the same static ``model``.
    """
    status = row.get("status") or STATUS_AVAILABLE
    available = status == STATUS_AVAILABLE
    kind = row.get("unavailable_kind") if not available else None
    reason = row.get("unavailable_reason") if not available else None

    device_model = row.get(target_key) or ""
    host_model = (host_hw or {}).get("model")
    # For an SBC the device *is* the host board, so its real identity is the
    # detected board model — use that instead of the static topology model (a
    # blanket "pi5" across the whole fleet). An MCU keeps its own board model
    # (the host is merely where it's plugged in). Falls back to the stored model
    # when the host hasn't been probed yet (e.g. an offline SBC).
    is_sbc = row.get("kind") == "sbc"
    model = host_model if (is_sbc and host_model) else device_model

    return {
        "target": row.get("build_target") or model or "",
        "model": model,
        "device_id": row.get("id"),
        "host_id": row.get("host_id"),
        "available": available,
        "status": status,
        "kind": kind,
        "reason": reason,
        "retry_after": row.get("retry_after") if not available else None,
        "host": host_hw,
    }
