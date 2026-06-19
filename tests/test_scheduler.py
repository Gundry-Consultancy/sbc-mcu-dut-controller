"""M1 tests: in-process scheduler and fake worker."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hil_controller.adapters.base import DeviceAdapter
from hil_controller.queue.events import EventBus
from hil_controller.queue.scheduler import Scheduler
from hil_controller.queue.worker import JobWorker, WorkerResult


@pytest.fixture
def event_bus():
    return EventBus()


@pytest.fixture
def fake_adapter():
    adapter = AsyncMock(spec=DeviceAdapter)
    adapter.acquire = AsyncMock(return_value=None)
    adapter.reset = AsyncMock(return_value=None)
    adapter.flash = AsyncMock(return_value=None)
    adapter.open_serial = AsyncMock(return_value=None)
    adapter.release = AsyncMock(return_value=None)
    return adapter


@pytest.mark.asyncio
async def test_event_bus_publish_and_subscribe(event_bus):
    received = []

    async def handler(event):
        received.append(event)

    event_bus.subscribe("job-123", handler)
    await event_bus.publish("job-123", {"kind": "state", "payload": {"state": "running"}})
    await asyncio.sleep(0)
    assert len(received) == 1
    event_bus.unsubscribe("job-123", handler)


@pytest.mark.asyncio
async def test_event_bus_no_handler_does_not_raise(event_bus):
    await event_bus.publish("nonexistent-job", {"kind": "log", "payload": {"msg": "hi"}})


@pytest.mark.asyncio
async def test_worker_runs_fake_adapter_to_finish(event_bus, fake_adapter):
    states = []

    async def on_event(ev):
        if ev.get("kind") == "state":
            states.append(ev["payload"]["state"])

    event_bus.subscribe("j1", on_event)

    worker = JobWorker(
        job_id="j1",
        adapter=fake_adapter,
        event_bus=event_bus,
        script="git-clone-and-run",
        params={"entry": "python", "args": ["-m", "pytest"]},
        payload={"kind": "fake"},
        timeouts={"total_s": 30},
    )
    result = await worker.run()

    assert result.state == "finished"
    assert result.result in ("pass", "fail", "error")
    assert "running" in states or "finished" in states
    event_bus.unsubscribe("j1", on_event)


@pytest.mark.asyncio
async def test_firmware_bench_runs_pipeline_from_params_without_payload_kind(
    event_bus, fake_adapter
):
    """firmware-bench drives its pipeline from params.firmware/stages — there is
    no top-level flash payload, so the worker must still call adapter.flash().
    (Regression: the CI HIL job submitted exactly this shape and the staged
    pipeline was skipped, leaving the job to only hold the device.)"""
    fake_adapter.run = AsyncMock(return_value="pass")

    worker = JobWorker(
        job_id="fwb1",
        adapter=fake_adapter,
        event_bus=event_bus,
        script="firmware-bench",
        params={
            "firmware": {"path": "/tmp/fw.combined.bin", "offset": "0x0"},
            "stages": [{"type": "flash"}, {"type": "inject_pixelwrite"}],
        },
        payload={},  # no kind — the firmware lives under params
        timeouts={"total_s": 30},
    )
    result = await worker.run()

    fake_adapter.flash.assert_awaited_once()
    assert result.state == "finished"


@pytest.mark.asyncio
async def test_firmware_bench_bare_hold_skips_flash(event_bus, fake_adapter):
    """A firmware-bench interactive hold with no firmware/stages must NOT flash —
    it goes straight to the hold (adapter.run())."""
    fake_adapter.run = AsyncMock(return_value="pass")

    worker = JobWorker(
        job_id="fwb2",
        adapter=fake_adapter,
        event_bus=event_bus,
        script="firmware-bench",
        params={},  # bare hold
        payload={},
        timeouts={"total_s": 30},
    )
    result = await worker.run()

    fake_adapter.flash.assert_not_awaited()
    assert result.state == "finished"


# --------------------------------------------------------------------------- #
# Log event emission from adapter output                                        #
# --------------------------------------------------------------------------- #


class _RunAdapter:
    """Minimal adapter that exposes captured run output."""

    def __init__(self, stdout: str = "", stderr: str = "", outcome: str = "pass") -> None:
        self._run_stdout = stdout
        self._run_stderr = stderr
        self._outcome = outcome

    async def acquire(self) -> None:
        pass

    async def release(self) -> None:
        pass

    async def run(self) -> str:
        return self._outcome


@pytest.mark.asyncio
async def test_worker_emits_stdout_as_log_event(event_bus):
    log_events = []

    async def on_event(ev):
        if ev.get("kind") == "log":
            log_events.append(ev)

    event_bus.subscribe("j-log1", on_event)
    worker = JobWorker(
        job_id="j-log1",
        adapter=_RunAdapter(stdout="2 passed\n"),
        event_bus=event_bus,
        script="pytest-suite",
        params={},
        payload={},
        timeouts={"total_s": 30},
    )
    result = await worker.run()
    event_bus.unsubscribe("j-log1", on_event)

    assert result.state == "finished"
    assert any(
        e["payload"].get("stream") == "stdout" and "2 passed" in e["payload"].get("msg", "")
        for e in log_events
    )


@pytest.mark.asyncio
async def test_worker_emits_stderr_as_log_event(event_bus):
    log_events = []

    async def on_event(ev):
        if ev.get("kind") == "log":
            log_events.append(ev)

    event_bus.subscribe("j-log2", on_event)
    worker = JobWorker(
        job_id="j-log2",
        adapter=_RunAdapter(stderr="DeprecationWarning\n"),
        event_bus=event_bus,
        script="pytest-suite",
        params={},
        payload={},
        timeouts={"total_s": 30},
    )
    await worker.run()
    event_bus.unsubscribe("j-log2", on_event)

    assert any(
        e["payload"].get("stream") == "stderr"
        and "DeprecationWarning" in e["payload"].get("msg", "")
        for e in log_events
    )


@pytest.mark.asyncio
async def test_worker_no_log_events_when_output_empty(event_bus):
    log_events = []

    async def on_event(ev):
        if ev.get("kind") == "log":
            log_events.append(ev)

    event_bus.subscribe("j-log3", on_event)
    worker = JobWorker(
        job_id="j-log3",
        adapter=_RunAdapter(stdout="", stderr=""),
        event_bus=event_bus,
        script="pytest-suite",
        params={},
        payload={},
        timeouts={"total_s": 30},
    )
    await worker.run()
    event_bus.unsubscribe("j-log3", on_event)

    stdout_events = [e for e in log_events if e["payload"].get("stream") == "stdout"]
    assert stdout_events == []


# --------------------------------------------------------------------------- #
# ProtoMQ observer integration                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_worker_activates_protomq_script_when_configured(event_bus):
    """Observer.activate_script is called with the script name from params."""
    log_events = []

    async def on_event(ev):
        log_events.append(ev)

    event_bus.subscribe("j-pmq1", on_event)

    mock_obs = AsyncMock()
    mock_obs.activate_script = AsyncMock(return_value={"status": "OK"})
    mock_obs.observe = AsyncMock(return_value=None)
    mock_obs.get_script_status = AsyncMock(
        return_value={"active_script": "demo", "completed_steps": ["checkin-response"]}
    )
    mock_obs.deactivate = AsyncMock()

    # _start_protomq_observer does a local import, so patch at the source module
    with patch("hil_controller.adapters.protomq_observer.ProtoMQObserver", return_value=mock_obs):
        worker = JobWorker(
            job_id="j-pmq1",
            adapter=_RunAdapter(stdout="1 passed"),
            event_bus=event_bus,
            script="pytest-suite",
            params={
                "protomq": {
                    "broker_host": "pi5-proto.local",
                    "script": "my-demo",
                }
            },
            payload={},
            timeouts={"total_s": 30},
        )
        await worker.run()

    event_bus.unsubscribe("j-pmq1", on_event)

    mock_obs.activate_script.assert_awaited_once_with("my-demo")


@pytest.mark.asyncio
async def test_worker_skips_protomq_when_no_script_configured(event_bus):
    """No observer is created when params.protomq.script is absent."""
    with patch("hil_controller.adapters.protomq_observer.ProtoMQObserver") as MockCls:
        worker = JobWorker(
            job_id="j-pmq2",
            adapter=_RunAdapter(),
            event_bus=event_bus,
            script="pytest-suite",
            params={},
            payload={},
            timeouts={"total_s": 30},
        )
        await worker.run()
        MockCls.assert_not_called()


# --------------------------------------------------------------------------- #
# Secrets purge                                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_worker_purges_secrets_from_db_after_finish(tmp_path, event_bus):
    """request_json.secrets values are redacted in the DB once the worker finishes."""
    import json

    from hil_controller.db.connection import get_db, get_job, init_db, insert_job

    db_file = str(tmp_path / "purge.db")
    await init_db(db_file)

    job_id = "purge-test-1"
    async with get_db(db_file) as db:
        await insert_job(
            db,
            job_id=job_id,
            request_json={
                "secrets": {"IO_KEY": "supersecret", "IO_USERNAME": "testuser"},
                "params": {},
                "payload": {},
            },
            secrets_profile="bench-protomq",
            exclusive_host=False,
        )

    worker = JobWorker(
        job_id=job_id,
        adapter=_RunAdapter(stdout="1 passed"),
        event_bus=event_bus,
        script="pytest-suite",
        params={},
        payload={},
        timeouts={"total_s": 30},
        db_path=db_file,
    )
    await worker.run()

    async with get_db(db_file) as db:
        row = await get_job(db, job_id)
    req = json.loads(row["request_json"])
    assert req["secrets"]["IO_KEY"] == "***"
    assert req["secrets"]["IO_USERNAME"] == "***"


@pytest.mark.asyncio
async def test_worker_purge_no_op_when_no_secrets(tmp_path, event_bus):
    """Worker does not crash when request_json has no secrets field."""
    import json

    from hil_controller.db.connection import get_db, get_job, init_db, insert_job

    db_file = str(tmp_path / "purge2.db")
    await init_db(db_file)

    job_id = "purge-test-2"
    async with get_db(db_file) as db:
        await insert_job(
            db,
            job_id=job_id,
            request_json={"params": {}, "payload": {}},
            secrets_profile="bench-protomq",
            exclusive_host=False,
        )

    worker = JobWorker(
        job_id=job_id,
        adapter=_RunAdapter(),
        event_bus=event_bus,
        script="pytest-suite",
        params={},
        payload={},
        timeouts={"total_s": 30},
        db_path=db_file,
    )
    result = await worker.run()
    assert result.state == "finished"


# --------------------------------------------------------------------------- #
# Deploy-log capture as a downloadable asset (+ secret redaction)               #
# --------------------------------------------------------------------------- #


from hil_controller.queue.worker import _redact_secrets


class _DeployAdapter:
    """git-source adapter exposing deploy output; can fail mid-deploy."""

    def __init__(self, *, stdout="", stderr="", source=None, fail=False) -> None:
        self.source = source or {"repo": "https://github.com/o/r.git", "ref": "main"}
        self._deploy_stdout = stdout
        self._deploy_stderr = stderr
        self._run_stdout = ""
        self._run_stderr = ""
        self._fail = fail

    async def acquire(self) -> None:
        pass

    async def release(self) -> None:
        pass

    async def deploy(self) -> None:
        if self._fail:
            raise RuntimeError("build failed")

    async def run(self) -> str:
        return "pass"


def test_redact_secrets_scrubs_url_creds_and_bare_tokens():
    s = "git clone https://ghp_ABCDEFGHIJKLMNOPQRST@github.com/o/r.git; tok=ghp_ABCDEFGHIJKLMNOPQRST"  # noqa: E501
    out = _redact_secrets(s)
    assert "ghp_ABCDEFGHIJKLMNOPQRST" not in out
    assert "https://<redacted>@github.com/o/r.git" in out


async def _run_git_source_worker(tmp_path, event_bus, adapter, job_id):
    from unittest.mock import patch as _patch

    from hil_controller.db.connection import get_db, init_db, insert_job

    db_file = str(tmp_path / f"{job_id}.db")
    await init_db(db_file)
    async with get_db(db_file) as db:
        await insert_job(
            db,
            job_id=job_id,
            request_json={"params": {}, "payload": {"kind": "git-source"}},
            secrets_profile="p",
            exclusive_host=False,
        )
    worker = JobWorker(
        job_id=job_id,
        adapter=adapter,
        event_bus=event_bus,
        script="git-clone-and-run",
        params={},
        payload={"kind": "git-source"},
        timeouts={"total_s": 30},
        db_path=db_file,
    )
    with _patch("hil_controller.config.resolve_jobs_dir", return_value=str(tmp_path / "jobs")):
        result = await worker.run()
    return db_file, result


async def _assets_for(db_file, job_id):
    from hil_controller.db.connection import get_db

    async with get_db(db_file) as db:
        async with db.execute(
            "SELECT * FROM assets WHERE job_id = ? AND kind = 'log'", (job_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


@pytest.mark.asyncio
async def test_deploy_log_captured_as_asset_with_redaction(tmp_path, event_bus):
    adapter = _DeployAdapter(
        source={
            "repo": "https://github.com/o/r.git",
            "ref": "main",
            "setup": [
                "bash",
                "-c",
                "git clone https://ghp_ABCDEFGHIJKLMNOPQRST@github.com/o/p.git",
            ],
        },
        stdout="cloning https://ghp_ABCDEFGHIJKLMNOPQRST@github.com/o/p.git\nbuilt ok\n",
    )
    db_file, result = await _run_git_source_worker(tmp_path, event_bus, adapter, "dep-ok")
    assert result.state == "finished"
    assets = await _assets_for(db_file, "dep-ok")
    assert len(assets) == 1
    from pathlib import Path as _P

    content = _P(assets[0]["path"]).read_text()
    assert "built ok" in content
    assert "ghp_ABCDEFGHIJKLMNOPQRST" not in content  # PAT scrubbed in the asset


@pytest.mark.asyncio
async def test_deploy_log_captured_even_when_deploy_fails(tmp_path, event_bus):
    adapter = _DeployAdapter(stdout="toolchain error: Dynconfig not exist\n", fail=True)
    db_file, result = await _run_git_source_worker(tmp_path, event_bus, adapter, "dep-fail")
    assert result.state == "error"
    assets = await _assets_for(db_file, "dep-fail")
    assert len(assets) == 1  # build log persisted despite the failure
    from pathlib import Path as _P

    assert "Dynconfig" in _P(assets[0]["path"]).read_text()


@pytest.mark.asyncio
async def test_cancel_job_cancels_active_worker_task(event_bus):
    """cancel_job() must cancel the running asyncio task, not just rely on a DB
    flag — interactive holds have no total timeout and won't stop otherwise."""
    sched = Scheduler(db_path=":memory:", event_bus=event_bus)

    started = asyncio.Event()

    async def _long_running():
        started.set()
        await asyncio.sleep(100)

    task = asyncio.create_task(_long_running())
    await started.wait()
    sched._active["job-x"] = task

    assert sched.cancel_job("job-x") is True
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()

    # Unknown / already-finished jobs are a no-op.
    assert sched.cancel_job("nope") is False
    assert sched.cancel_job("job-x") is False
