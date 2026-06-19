"""Tests for POST /v1/firmware (upload)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

FW = b"\x1a\x09uploaded-combined-bin"


@pytest.mark.asyncio
async def test_upload_requires_auth(client):
    r = await client.post("/v1/firmware?filename=x.bin", content=FW)
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_upload_stores_and_returns_path(authed_client, app):
    r = await authed_client.post(
        "/v1/firmware?filename=wippersnapper.qtpy_esp32s3_n4r2.fatfs.combined.bin", content=FW
    )
    assert r.status_code == 200
    rec = r.json()
    assert rec["size_bytes"] == len(FW)
    assert rec["sha256"] == hashlib.sha256(FW).hexdigest()
    assert rec["filename"].endswith(".combined.bin")
    assert Path(rec["path"]).read_bytes() == FW  # landed on the controller filesystem

    # Tracked as a kind='firmware' asset (job_id NULL until a job flashes it) with purge_at.
    from hil_controller.db.connection import get_db

    async with get_db(app.state.db_path) as db:
        cur = await db.execute(
            "SELECT kind, job_id, purge_at, size_bytes FROM assets WHERE id=?", (rec["id"],)
        )
        row = await cur.fetchone()
    assert row is not None
    assert row["kind"] == "firmware"
    assert row["job_id"] is None
    assert row["purge_at"]  # set so it gets cleaned up eventually
    assert row["size_bytes"] == len(FW)


@pytest.mark.asyncio
async def test_upload_empty_400(authed_client):
    r = await authed_client.post("/v1/firmware?filename=x.bin", content=b"")
    assert r.status_code == 400
