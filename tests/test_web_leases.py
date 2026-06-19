"""Tests for the /ui/leases page + body fragment + force-release (M3.5).

The page consumes the existing /v1/leases data via a server-side helper
that joins lease rows against the devices/hosts/jobs tables — so these
tests seed a couple of leases and assert the rendered HTML carries the
expected fields.
"""

from __future__ import annotations

import pytest

from hil_controller.db.connection import get_db
from hil_controller.queue.leases import acquire

TOKEN = "test-token-for-ci"
COOKIE = {"hil_token": TOKEN}


async def _seed_host_device(db_path: str) -> None:
    async with get_db(db_path) as db:
        await db.execute(
            """INSERT INTO hosts (id, role, addr, transport, ssh_user, ssh_key_path,
                   max_concurrent_jobs, capabilities_json, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "rpi-displays",
                "microcontroller-fleet",
                "192.168.1.234",
                "ssh",
                "pi",
                "/etc/hil/keys/rpi-displays",
                None,
                "[]",
                "available",
            ),
        )
        await db.execute(
            """INSERT INTO devices (id, host_id, hub_host_id, hub_port_path, kind, model,
                   capabilities_json, status, pool)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "mcu-feather-esp32s3-revtft",
                "rpi-displays",
                "rpi-displays",
                "1-1.1.1.4",
                "microcontroller",
                "feather-esp32s3-revtft",
                "[]",
                "available",
                "public",
            ),
        )
        await db.commit()


# --------------------------------------------------------------------------- #
# Auth guard                                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_leases_page_redirects_without_cookie(client) -> None:
    r = await client.get("/ui/leases", follow_redirects=False)
    assert r.status_code == 303
    assert "/ui/login" in r.headers["location"]


@pytest.mark.asyncio
async def test_leases_body_redirects_without_cookie(client) -> None:
    r = await client.get("/ui/leases/body", follow_redirects=False)
    assert r.status_code == 303


# --------------------------------------------------------------------------- #
# Empty state                                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_leases_page_empty_state(client) -> None:
    r = await client.get("/ui/leases", cookies=COOKIE)
    assert r.status_code == 200
    assert "No active leases" in r.text
    # nav-active styling on the new "Leases" tab
    assert "/ui/leases" in r.text


# --------------------------------------------------------------------------- #
# Populated state                                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_leases_page_shows_active_lease_for_device(client, app) -> None:
    db_path = app.state.db_path
    await _seed_host_device(db_path)
    await acquire(
        db_path,
        kind="exclusive_device",
        device_id="mcu-feather-esp32s3-revtft",
        hub_host_id="rpi-displays",
        job_id="job-abc12345-xyz",
    )

    r = await client.get("/ui/leases", cookies=COOKIE)
    assert r.status_code == 200
    body = r.text
    # device id rendered (linked to the device's form page)
    assert "mcu-feather-esp32s3-revtft" in body
    # host id rendered alongside its addr
    assert "rpi-displays" in body
    assert "192.168.1.234" in body
    # job-id prefix appears (truncated to 8 chars)
    assert "job-abc1" in body
    # force-release button present and points at the right id
    assert "Force release" in body
    assert 'hx-delete="/ui/leases/1"' in body


@pytest.mark.asyncio
async def test_leases_body_returns_just_the_fragment(client, app) -> None:
    db_path = app.state.db_path
    await _seed_host_device(db_path)
    await acquire(
        db_path,
        kind="exclusive_hub",
        hub_host_id="rpi-displays",
        job_id="job-hub-lock",
    )

    r = await client.get("/ui/leases/body", cookies=COOKIE)
    assert r.status_code == 200
    # Body fragment is just <tr>...</tr> rows, no <html>/<head>/<nav>
    assert "<html" not in r.text.lower()
    assert "<nav" not in r.text.lower()
    # but it carries the row content
    assert "rpi-displays" in r.text
    assert "job-hub-" in r.text  # truncated job id


# --------------------------------------------------------------------------- #
# Force release                                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_force_release_clears_lease(client, app) -> None:
    db_path = app.state.db_path
    await _seed_host_device(db_path)
    lease = await acquire(
        db_path,
        kind="exclusive_device",
        device_id="mcu-feather-esp32s3-revtft",
        hub_host_id="rpi-displays",
        job_id="job-release-me",
    )

    r = await client.delete(f"/ui/leases/{lease['id']}", cookies=COOKIE)
    assert r.status_code == 200
    # Empty body so HTMX swaps the row out of the table cleanly.
    assert r.text == ""

    # After release, the page reverts to the empty state.
    r2 = await client.get("/ui/leases", cookies=COOKIE)
    assert "No active leases" in r2.text
