"""Tests for JobWorker._harvest_artifacts (params.collect_artifacts).

A non-interactive job (e.g. a pytest-suite display test) can declare a list of
glob patterns under ``params.collect_artifacts``; the worker copies the matches
into the job dir and registers each as a downloadable asset so CI can pull them
via GET /v1/jobs/{id}/assets. ``.log``/``.txt`` register as ``log``, everything
else as ``file``.
"""

from unittest.mock import AsyncMock

import pytest

from hil_controller import config
from hil_controller.adapters.base import DeviceAdapter
from hil_controller.db.connection import get_db, init_db
from hil_controller.queue.events import EventBus
from hil_controller.queue.worker import JobWorker


def _worker(db_file, params):
    return JobWorker(
        job_id="job-harvest",
        adapter=AsyncMock(spec=DeviceAdapter),
        event_bus=EventBus(),
        script="pytest-suite",
        params=params,
        payload={},
        timeouts={},
        db_path=db_file,
    )


@pytest.mark.asyncio
async def test_harvest_copies_files_and_registers_assets(tmp_path, monkeypatch):
    db_file = str(tmp_path / "t.db")
    await init_db(db_file)
    jobs_dir = tmp_path / "jobs"
    monkeypatch.setattr(config, "resolve_jobs_dir", lambda: str(jobs_dir))

    src = tmp_path / "out"
    src.mkdir()
    (src / "01_after_add.jpg").write_bytes(b"\xff\xd8\xff\xe0jpeg-bytes")
    (src / "02_after_write.jpg").write_bytes(b"\xff\xd8\xff\xe0jpeg-bytes")
    (src / "protomq.log").write_text("broker line 1\nbroker line 2\n", encoding="utf-8")

    worker = _worker(
        db_file,
        {"collect_artifacts": [str(src / "*.jpg"), str(src / "protomq.log")]},
    )
    await worker._harvest_artifacts()  # pylint: disable=protected-access

    job_dir = jobs_dir / "job-harvest"
    assert (job_dir / "01_after_add.jpg").is_file()
    assert (job_dir / "02_after_write.jpg").is_file()
    assert (job_dir / "protomq.log").read_text(encoding="utf-8").startswith("broker line 1")

    async with get_db(db_file) as db:
        async with db.execute(
            "SELECT filename, kind FROM assets WHERE job_id = ? ORDER BY filename",
            ("job-harvest",),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    by_name = {r["filename"]: r["kind"] for r in rows}
    assert by_name == {
        "01_after_add.jpg": "file",
        "02_after_write.jpg": "file",
        "protomq.log": "log",
    }


@pytest.mark.asyncio
async def test_harvest_noop_without_param(tmp_path, monkeypatch):
    db_file = str(tmp_path / "t.db")
    await init_db(db_file)
    monkeypatch.setattr(config, "resolve_jobs_dir", lambda: str(tmp_path / "jobs"))

    worker = _worker(db_file, {})
    await worker._harvest_artifacts()  # pylint: disable=protected-access

    async with get_db(db_file) as db:
        async with db.execute("SELECT COUNT(*) AS c FROM assets") as cur:
            row = await cur.fetchone()
    assert row["c"] == 0


@pytest.mark.asyncio
async def test_harvest_skips_missing_globs(tmp_path, monkeypatch):
    db_file = str(tmp_path / "t.db")
    await init_db(db_file)
    monkeypatch.setattr(config, "resolve_jobs_dir", lambda: str(tmp_path / "jobs"))

    worker = _worker(db_file, {"collect_artifacts": [str(tmp_path / "nope" / "*.jpg")]})
    await worker._harvest_artifacts()  # pylint: disable=protected-access  # must not raise

    async with get_db(db_file) as db:
        async with db.execute("SELECT COUNT(*) AS c FROM assets") as cur:
            row = await cur.fetchone()
    assert row["c"] == 0
