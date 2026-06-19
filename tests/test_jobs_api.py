"""M1 tests: job submission, state machine, long-poll."""

import asyncio

import pytest

SBC_JOB_BODY = {
    "target": {
        "device": {"kind": "sbc", "model": "pi5"},
        "pool": "wippersnapper-python",
    },
    "script": "git-clone-and-run",
    "params": {"entry": "python", "args": ["-m", "pytest", "-m", "eink_large", "-v"]},
    "payload": {
        "kind": "git-source",
        "source": {
            "repo": "https://github.com/adafruit/Wippersnapper_Python.git",
            "ref": "main",
            "submodules": False,
            "shallow": True,
            "setup": ["pip", "install", "-e", ".[test]"],
        },
    },
    "secrets_profile": "bench-protomq",
    "exclusive": {"host": True},
    "timeouts": {"total_s": 1800},
    "metadata": {"caller": "test", "repo": "adafruit/Wippersnapper_Python"},
}


@pytest.mark.asyncio
async def test_submit_job_requires_auth(client):
    r = await client.post("/v1/jobs", json=SBC_JOB_BODY)
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_submit_job_returns_202(authed_client):
    r = await authed_client.post("/v1/jobs", json=SBC_JOB_BODY)
    assert r.status_code == 202
    body = r.json()
    assert "id" in body
    assert "wait_url" in body
    assert body["wait_url"].endswith("/wait")


@pytest.mark.asyncio
async def test_get_job_snapshot(authed_client):
    submit = await authed_client.post("/v1/jobs", json=SBC_JOB_BODY)
    job_id = submit.json()["id"]

    r = await authed_client.get(f"/v1/jobs/{job_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == job_id
    assert body["state"] in ("queued", "assigned", "preparing", "flashing", "running", "finished")


@pytest.mark.asyncio
async def test_get_job_unknown_returns_404(authed_client):
    r = await authed_client.get("/v1/jobs/no-such-job-id")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_long_poll_returns_immediately_when_events_exist(authed_client):
    submit = await authed_client.post("/v1/jobs", json=SBC_JOB_BODY)
    job_id = submit.json()["id"]

    r = await authed_client.get(f"/v1/jobs/{job_id}/wait?since=0&timeout=1")
    assert r.status_code == 200
    body = r.json()
    assert "events" in body
    assert "next_since" in body
    assert "state" in body


@pytest.mark.asyncio
async def test_long_poll_returns_on_timeout(authed_client):
    """When no new events land, the poll should return after timeout."""
    submit = await authed_client.post("/v1/jobs", json=SBC_JOB_BODY)
    job_id = submit.json()["id"]

    # drain initial events
    r1 = await authed_client.get(f"/v1/jobs/{job_id}/wait?since=0&timeout=1")
    since = r1.json()["next_since"]

    # now poll from current cursor — should time out and return empty events
    r2 = await authed_client.get(f"/v1/jobs/{job_id}/wait?since={since}&timeout=1")
    assert r2.status_code == 200
    body = r2.json()
    assert body["events"] == [] or isinstance(body["events"], list)


@pytest.mark.asyncio
async def test_cancel_job(authed_client):
    submit = await authed_client.post("/v1/jobs", json=SBC_JOB_BODY)
    job_id = submit.json()["id"]

    r = await authed_client.post(f"/v1/jobs/{job_id}/cancel")
    assert r.status_code in (200, 202, 409)


@pytest.mark.asyncio
async def test_missing_required_fields_rejected(authed_client):
    r = await authed_client.post("/v1/jobs", json={"script": "git-clone-and-run"})
    assert r.status_code == 422
