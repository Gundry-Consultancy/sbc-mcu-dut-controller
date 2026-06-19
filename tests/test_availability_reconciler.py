"""Tests for the availability reconciler (self-rectification of temp outages)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from hil_controller.availability_reconciler import reconcile_once
from hil_controller.db.connection import get_db, init_db


def _t(s: int = 0) -> datetime:
    return datetime(2026, 6, 14, 16, 0, 0, tzinfo=UTC) + timedelta(seconds=s)


@pytest.fixture
async def db_path(tmp_path):
    path = str(tmp_path / "reconcile.db")
    await init_db(path)
    return path


async def _seed(
    db_path,
    *,
    device_id,
    status="unavailable",
    kind="temporary",
    retry_attempts=0,
    retry_after=None,
):
    async with get_db(db_path) as db:
        await db.execute(
            "INSERT INTO devices (id, host_id, kind, model, status, "
            "unavailable_kind, unavailable_reason, unavailable_since, "
            "retry_attempts, retry_after) "
            "VALUES (?, 'h1', 'mcu', 'feather', ?, ?, 'wedged', ?, ?, ?)",
            (device_id, status, kind, _t(0).isoformat(), retry_attempts, retry_after),
        )
        await db.commit()


async def _row(db_path, device_id) -> dict:
    async with get_db(db_path) as db:
        cur = await db.execute("SELECT * FROM devices WHERE id = ?", (device_id,))
        return dict(await cur.fetchone())


async def _always(_device):
    return True


async def _never(_device):
    return False


@pytest.mark.asyncio
async def test_heals_on_probe_success(db_path):
    await _seed(db_path, device_id="d1")
    await reconcile_once(db_path, probe=_always, max_attempts=3, window_s=180, now=_t(0))

    row = await _row(db_path, "d1")
    assert row["status"] == "available"
    assert row["unavailable_kind"] is None
    assert row["unavailable_reason"] is None
    assert row["unavailable_since"] is None
    assert row["retry_attempts"] == 0
    assert row["retry_after"] is None
    assert row["last_checked_at"] is not None


@pytest.mark.asyncio
async def test_increments_and_backs_off_on_failure(db_path):
    await _seed(db_path, device_id="d1")
    await reconcile_once(db_path, probe=_never, max_attempts=3, window_s=180, now=_t(0))

    row = await _row(db_path, "d1")
    assert row["status"] == "unavailable"
    assert row["unavailable_kind"] == "temporary"
    assert row["retry_attempts"] == 1
    # backoff = 180/3 = 60s past now.
    assert row["retry_after"] == _t(60).isoformat()
    assert row["last_checked_at"] is not None


@pytest.mark.asyncio
async def test_waits_until_retry_after(db_path):
    """Not yet due → probe is not run, nothing changes."""
    calls = []

    async def counting_probe(device):
        calls.append(device["id"])
        return False

    await _seed(db_path, device_id="d1", retry_attempts=1, retry_after=_t(60).isoformat())
    await reconcile_once(db_path, probe=counting_probe, max_attempts=3, window_s=180, now=_t(10))

    assert calls == []
    row = await _row(db_path, "d1")
    assert row["retry_attempts"] == 1  # untouched


@pytest.mark.asyncio
async def test_gives_up_after_budget(db_path):
    """Budget exhausted → probe never runs again, stays temporary."""
    calls = []

    async def counting_probe(device):
        calls.append(device["id"])
        return True  # would heal, but must not even be called

    await _seed(db_path, device_id="d1", retry_attempts=3)
    await reconcile_once(db_path, probe=counting_probe, max_attempts=3, window_s=180, now=_t(999))

    assert calls == []
    row = await _row(db_path, "d1")
    assert row["status"] == "unavailable"
    assert row["unavailable_kind"] == "temporary"
    assert row["retry_attempts"] == 3


@pytest.mark.asyncio
async def test_never_touches_permanent(db_path):
    calls = []

    async def counting_probe(device):
        calls.append(device["id"])
        return True

    await _seed(db_path, device_id="d-perm", kind="permanent")
    await reconcile_once(db_path, probe=counting_probe, max_attempts=3, window_s=180, now=_t(0))

    assert calls == []
    row = await _row(db_path, "d-perm")
    assert row["status"] == "unavailable"
    assert row["unavailable_kind"] == "permanent"
    assert row["retry_attempts"] == 0


@pytest.mark.asyncio
async def test_full_budget_then_give_up(db_path):
    """Three failing attempts spend the budget, then the device is left alone."""
    await _seed(db_path, device_id="d1")

    # Attempt 1 (due immediately): retry_attempts 0 -> 1, retry_after = now+60.
    await reconcile_once(db_path, probe=_never, max_attempts=3, window_s=180, now=_t(0))
    assert (await _row(db_path, "d1"))["retry_attempts"] == 1

    # Attempt 2 at t=60 (>= retry_after): 1 -> 2.
    await reconcile_once(db_path, probe=_never, max_attempts=3, window_s=180, now=_t(60))
    assert (await _row(db_path, "d1"))["retry_attempts"] == 2

    # Attempt 3 at t=120: 2 -> 3.
    await reconcile_once(db_path, probe=_never, max_attempts=3, window_s=180, now=_t(120))
    assert (await _row(db_path, "d1"))["retry_attempts"] == 3

    # Now give_up: a healing probe must not be invoked.
    calls = []

    async def counting_probe(device):
        calls.append(device["id"])
        return True

    await reconcile_once(db_path, probe=counting_probe, max_attempts=3, window_s=180, now=_t(999))
    assert calls == []
    assert (await _row(db_path, "d1"))["status"] == "unavailable"
