"""Devices page: outage detail is visible, operator status edits stay consistent."""

from __future__ import annotations

import os

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from hil_controller.db.connection import get_db

COOKIE = {"hil_token": "test-token-for-ci"}


@pytest_asyncio.fixture
async def app_db(tmp_path):
    from hil_controller.main import create_app

    db_file = str(tmp_path / "avail.db")
    os.environ["HIL_DB_PATH"] = db_file
    app = create_app(db_path=db_file, topology_file="")
    async with app.router.lifespan_context(app):
        async with get_db(db_file) as db:
            await db.execute(
                "INSERT INTO hosts (id, addr, capabilities_json) VALUES ('h','a','[]')"
            )
            for dev_id, status, kind, reason, attempts in (
                ("d-ok", "available", None, None, 0),
                ("d-temp", "unavailable", "temporary", "USB enumeration wedged", 3),
                ("d-perm", "unavailable", "permanent", "removed from the fleet", 0),
            ):
                await db.execute(
                    "INSERT INTO devices (id, host_id, kind, capabilities_json, status, "
                    "unavailable_kind, unavailable_reason, unavailable_since, retry_attempts, "
                    "retry_after) VALUES (?, 'h', 'microcontroller', '[]', ?, ?, ?, ?, ?, ?)",
                    (
                        dev_id,
                        status,
                        kind,
                        reason,
                        "2026-10-01T10:00:00+00:00" if kind else None,
                        attempts,
                        "2026-10-17T12:00:00+00:00" if kind == "temporary" else None,
                    ),
                )
            await db.commit()
        yield app, db_file


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=COOKIE)


async def _row(db_file: str, device_id: str):
    async with get_db(db_file) as db:
        async with db.execute("SELECT * FROM devices WHERE id = ?", (device_id,)) as cur:
            return await cur.fetchone()


async def test_devices_table_shows_outage_detail_inline(app_db):
    app, _ = app_db
    async with _client(app) as c:
        page = await c.get("/ui/devices")
    text = page.text
    assert "USB enumeration wedged" in text and "removed from the fleet" in text
    assert "unavailable · temporary" in text and "unavailable · permanent" in text
    assert "3 attempt(s)" in text and "2026-10-17T12:00:00" in text
    assert "2 unavailable" in text and "Retry all temporary" in text


async def test_retry_all_resets_only_temporary_outages(app_db):
    app, db_file = app_db
    async with get_db(db_file) as db:
        await db.execute("UPDATE devices SET retry_attempts=5 WHERE id='d-perm'")
        await db.commit()
    async with _client(app) as c:
        resp = await c.post("/ui/devices/availability/retry-all")
    assert resp.headers.get("HX-Refresh") == "true"
    temp, perm = await _row(db_file, "d-temp"), await _row(db_file, "d-perm")
    assert temp["retry_attempts"] == 0 and temp["retry_after"] is None
    assert perm["retry_attempts"] == 5


async def test_form_marks_unavailable_keeps_since_and_available_clears(app_db):
    app, db_file = app_db
    base = {"host_id": "h", "kind": "microcontroller", "power_control_present": "1"}
    async with _client(app) as c:
        form = await c.get("/ui/devices/d-ok/form")
        assert 'name="unavailable_kind"' in form.text
        assert "offline" not in form.text.split('id="f-dev-status"')[1].split("</select>")[0]
        await c.post(
            "/ui/devices/d-ok",
            data={
                **base,
                "status": "unavailable",
                "unavailable_kind": "permanent",
                "unavailable_reason": "sent for rework",
            },
        )
        row = await _row(db_file, "d-ok")
        assert (row["status"], row["unavailable_kind"], row["unavailable_reason"]) == (
            "unavailable",
            "permanent",
            "sent for rework",
        )
        since = row["unavailable_since"]
        assert since
        await c.post(
            "/ui/devices/d-ok",
            data={
                **base,
                "status": "unavailable",
                "unavailable_kind": "temporary",
                "unavailable_reason": "back soon",
            },
        )
        row = await _row(db_file, "d-ok")
        assert row["unavailable_since"] == since and row["unavailable_kind"] == "temporary"
        await c.post("/ui/devices/d-temp", data={**base, "status": "available"})
    row = await _row(db_file, "d-temp")
    assert row["status"] == "available"
    assert row["unavailable_kind"] is None and row["unavailable_reason"] is None
    assert row["unavailable_since"] is None and row["retry_attempts"] == 0
