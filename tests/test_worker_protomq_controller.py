"""JobWorker._maybe_launch_controller_protomq: launch protomq on the controller
for a pytest-suite job (params.protomq.launch_on == "controller") and inject the
broker host into the run env so a remote SBC test connects back to it."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from hil_controller import config
from hil_controller.adapters.base import DeviceAdapter
from hil_controller.queue.events import EventBus
from hil_controller.queue.worker import JobWorker


def _worker(params):
    return JobWorker(
        job_id="j-pmq",
        adapter=AsyncMock(spec=DeviceAdapter),
        event_bus=EventBus(),
        script="pytest-suite",
        params=params,
        payload={},
        timeouts={},
        db_path=None,
    )


@pytest.mark.asyncio
async def test_launch_controller_protomq_injects_env(monkeypatch):
    fake = MagicMock()
    fake.clone_and_build = AsyncMock()
    fake.start = AsyncMock()
    fake.stop = AsyncMock()
    fake.mqtt_port, fake.api_port = 1884, 5173
    monkeypatch.setattr(
        "hil_controller.adapters.protomq_launcher.ProtomqLauncher",
        MagicMock(return_value=fake),
    )
    monkeypatch.setattr(config, "resolve_jobs_dir", lambda: "/tmp/jd")

    worker = _worker({"protomq": {"launch_on": "controller", "script": "demo"}})
    await worker._maybe_launch_controller_protomq()  # pylint: disable=protected-access

    env = worker.params["extra_env"]
    ip = config.get_settings().controller_ip
    assert env["PROTOMQ_RUN_EXTERNALLY"] == "1"
    assert env["PROTOMQ_HOST"] == ip and env["MQTT_HOST"] == ip
    assert env["PROTOMQ_PORT"] == "1884" and env["MQTT_PORT"] == "1884"
    assert env["PROTOMQ_PATH"]  # required by WS-Python defaults when external
    assert worker._ctrl_protomq is fake  # pylint: disable=protected-access
    fake.clone_and_build.assert_awaited_once()
    fake.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_launch_when_not_requested(monkeypatch):
    monkeypatch.setattr(
        "hil_controller.adapters.protomq_launcher.ProtomqLauncher",
        MagicMock(side_effect=AssertionError("must not construct launcher")),
    )
    worker = _worker({"protomq": {"broker_host": "127.0.0.1"}})  # no launch_on
    await worker._maybe_launch_controller_protomq()  # pylint: disable=protected-access
    assert worker._ctrl_protomq is None  # pylint: disable=protected-access
    assert "PROTOMQ_HOST" not in worker.params.get("extra_env", {})
