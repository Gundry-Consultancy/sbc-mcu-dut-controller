"""Tests for the read-only job-assets API (list + download)."""

from __future__ import annotations

import pytest

from hil_controller.db.connection import get_db

MINIMAL_JOB = {
    "target": {"device": {"kind": "sbc", "model": "pi5"}, "pool": "wippersnapper-python"},
    "script": "git-clone-and-run",
    "params": {"entry": "python", "args": ["-c", "print(1)"]},
    "payload": {
        "kind": "git-source",
        "source": {"repo": "https://github.com/adafruit/Wippersnapper_Python.git", "ref": "main"},
    },
    "timeouts": {"total_s": 600},
}


async def _insert_asset(db_path, *, asset_id, job_id, filename, path, kind="log", size=0):
    async with get_db(db_path) as db:
        await db.execute(
            "INSERT INTO assets (id, filename, path, url, size_bytes, kind, job_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (asset_id, filename, path, None, size, kind, job_id, "2026-01-01T00:00:00Z"),
        )
        await db.commit()


@pytest.mark.asyncio
async def test_list_assets_requires_auth(client):
    r = await client.get("/v1/jobs/whatever/assets")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_assets_unknown_job_404(authed_client):
    r = await authed_client.get("/v1/jobs/no-such-job/assets")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_list_and_download_asset(authed_client, app, tmp_path):
    job_id = (await authed_client.post("/v1/jobs", json=MINIMAL_JOB)).json()["id"]
    logf = tmp_path / "serial.log"
    logf.write_text("BOOT ok\nPIXELWRITE_VERDICT rebooted=true pin=D0 color=200\n")
    await _insert_asset(
        app.state.db_path,
        asset_id="a1",
        job_id=job_id,
        filename="serial.log",
        path=str(logf),
        kind="log",
        size=logf.stat().st_size,
    )

    r = await authed_client.get(f"/v1/jobs/{job_id}/assets")
    assert r.status_code == 200
    assets = r.json()["assets"]
    assert len(assets) == 1
    assert assets[0]["filename"] == "serial.log"
    assert assets[0]["kind"] == "log"
    assert assets[0]["size_bytes"] == logf.stat().st_size

    d = await authed_client.get(f"/v1/jobs/{job_id}/assets/a1/download")
    assert d.status_code == 200
    assert "PIXELWRITE_VERDICT rebooted=true" in d.text


@pytest.mark.asyncio
async def test_download_requires_auth(client):
    r = await client.get("/v1/jobs/j/assets/a/download")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_download_unknown_asset_404(authed_client):
    job_id = (await authed_client.post("/v1/jobs", json=MINIMAL_JOB)).json()["id"]
    r = await authed_client.get(f"/v1/jobs/{job_id}/assets/nope/download")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_download_missing_file_on_disk_404(authed_client, app):
    job_id = (await authed_client.post("/v1/jobs", json=MINIMAL_JOB)).json()["id"]
    await _insert_asset(
        app.state.db_path,
        asset_id="gone",
        job_id=job_id,
        filename="x.log",
        path="/no/such/path/x.log",
        kind="log",
    )
    r = await authed_client.get(f"/v1/jobs/{job_id}/assets/gone/download")
    assert r.status_code == 404
