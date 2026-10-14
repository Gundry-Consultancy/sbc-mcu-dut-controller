"""Protocol and controller-contract tests; no real hardware or provider keys."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.memory import create_connected_server_and_client_session

from hil_controller.job_profiles import PUBLIC_SECRETS, AgentJob, build_job
from hil_controller.mcp_server import ControllerClient, SkillStore, create_server, http_app


def spec(**changes):
    return AgentJob.model_validate(
        {"runtime": "wippersnapper-arduino", "device_id": "dut", **changes}
    )


def test_public_defaults_do_not_read_environment(monkeypatch):
    monkeypatch.setenv("IO_KEY", "never-use-operator-key")
    request = build_job(spec())
    assert request["secrets"] == PUBLIC_SECRETS
    assert request["params"]["stages"][-1] == {"type": "verify_checkin"}
    assert "firmware" not in request["params"]


@pytest.mark.parametrize(
    "mode,host",
    [("production", "io.adafruit.com"), ("staging", "io.adafruit.us"), ("dev-lan", "dev.local")],
)
@pytest.mark.parametrize(
    "runtime", ["wippersnapper-arduino", "wippersnapper-python", "circuitpython"]
)
def test_external_routing(mode, host, runtime):
    extra = {"broker_host": host, "broker_port": 1883} if mode == "dev-lan" else {}
    extra.update(
        {
            "source": {"repo": "https://example.org/client.git", "ref": "main"},
            "params": {"entry": "python", "args": ["test_live.py"]},
        }
        if runtime == "wippersnapper-python"
        else {}
    )
    if runtime == "circuitpython":
        extra["files"] = {"code.py": "print('success')"}
    request = build_job(
        spec(
            runtime=runtime,
            broker=mode,
            expect_serial="success",
            secrets={"IO_USERNAME": "account", "IO_KEY": "live-test-example"},
            **extra,
        )
    )
    params = request["params"]
    assert "protomq" not in params
    assert "launch_protomq" not in params
    if runtime == "wippersnapper-python":
        assert params["extra_env"]["MQTT_HOST"] == host
    else:
        writes = [s for s in params["stages"] if s["type"].startswith("write_")]
        assert writes[0]["io_url"] == host
        assert writes[0]["io_port"] == (1883 if mode == "dev-lan" else 8883)


@pytest.mark.parametrize(
    "changes",
    [
        {"broker": "production"},
        {"broker": "staging", "secrets": {"IO_KEY": "key"}},
        {"broker": "dev-lan", "secrets": {"IO_USERNAME": "user", "IO_KEY": "key"}},
        {"broker_host": "io.adafruit.com"},
        {"secrets": {"IO_KEY": "live-key"}},
        {"params": {"protomq": {"launch_on": "controller"}}},
        {"params": {"extra_env": {"MQTT_HOST": "wrong"}}},
        {"params": {"stages": []}},
        {"params": {"stages": [{"type": "write_secrets_msc", "io_url": "wrong"}]}},
    ],
)
def test_bad_or_ambiguous_credentials_and_routing_rejected(changes):
    with pytest.raises(ValueError):
        build_job(spec(**changes))


def test_python_default_uses_controller_broker():
    request = build_job(
        spec(
            runtime="wippersnapper-python",
            source={"repo": "https://example.org/client.git", "ref": "commit"},
            params={"entry": "python", "args": ["test_checkin.py"]},
        )
    )
    assert request["script"] == "pytest-suite"
    assert request["params"]["protomq"] == {"launch_on": "controller"}
    assert request["params"]["secrets_format"] == "env"


def test_circuitpython_local_broker_precedes_deployment():
    request = build_job(
        spec(runtime="circuitpython", files={"code.py": "print('ok')"}, expect_serial="ok")
    )
    stages = request["params"]["stages"]
    assert stages[0]["type"] == "launch_protomq"
    assert stages[-1]["type"] == "assert_serial"
    assert not any(s["type"] == "write_secrets_msc" for s in stages)


def test_live_skills_update_and_traversal(tmp_path):
    path = tmp_path / "hil-new" / "SKILL.md"
    path.parent.mkdir()
    path.write_text("first")
    skills = SkillStore(tmp_path)
    before = skills.catalog()
    path.write_text("updated next week")
    assert before != skills.catalog()
    assert skills.read("hil-new") == "updated next week"
    with pytest.raises(ValueError):
        skills.read("../run/controller.env")


async def test_protocol_discovery_preview_submit_poll_and_assets():
    received = []

    def backend(request):
        received.append(request)
        assert request.headers["authorization"] == "Bearer controller-example-token"
        if request.url.path.endswith("/wait"):
            return httpx.Response(
                200, json={"state": "finished", "result": "pass", "next_since": 7}
            )
        if request.url.path.endswith("/download"):
            return httpx.Response(200, text="SERIAL_ASSERT_VERDICT ok=true")
        return httpx.Response(202, json={"id": "job-1"})

    server = create_server(
        ControllerClient(
            "http://bench", "controller-example-token", transport=httpx.MockTransport(backend)
        )
    )
    async with create_connected_server_and_client_session(server) as session:
        listing = await session.list_tools()
        names = {t.name: t for t in listing.tools}
        assert names["submit_job"].annotations.destructiveHint
        assert names["prepare_job"].annotations.readOnlyHint
        assert "free4all" in json.dumps(names["submit_job"].inputSchema)
        preview = await session.call_tool("prepare_job", {"job": spec().model_dump(mode="json")})
        assert not preview.isError
        assert received == []
        submitted = await session.call_tool("submit_job", {"job": spec().model_dump(mode="json")})
        assert not submitted.isError
        assert json.loads(received[0].content)["secrets"] == PUBLIC_SECRETS
        waited = await session.call_tool("wait_job", {"job_id": "job-1", "since": 3, "timeout": 1})
        assert not waited.isError
        assert received[-1].url.params["since"] == "3"
        asset = await session.call_tool("read_asset", {"job_id": "job-1", "asset_id": "proof"})
        assert not asset.isError
        assert "ok=true" in asset.content[0].text


async def test_http_transport_auth_and_real_mcp_initialize():
    server = create_server(ControllerClient("http://bench", "backend-token"))
    app = http_app(server, "mcp-test-token")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8081",
        ) as client,
    ):
        for verb in ("get", "post", "delete"):
            assert (await getattr(client, verb)("/mcp")).status_code == 401
        client.headers["Authorization"] = "Bearer mcp-test-token"
        async with streamable_http_client("http://127.0.0.1:8081/mcp", http_client=client) as (
            r,
            w,
            _,
        ):
            async with ClientSession(r, w) as session:
                initialized = await session.initialize()
                assert "free4all" in initialized.instructions
                result = await session.call_tool("job_guide")
                assert not result.isError


async def test_stdio_subprocess():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "hil_controller.mcp_server"],
        env={**os.environ, "HIL_API_TOKEN": "test-controller-token"},
    )
    async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
        result = await session.initialize()
        assert result.serverInfo.name == "HIL bench"
        assert not (await session.call_tool("list_skills")).isError


async def test_error_body_does_not_leak_or_retry():
    calls = []

    def backend(request):
        calls.append(request)
        return httpx.Response(422, json={"detail": "private-credential"})

    client = ControllerClient("http://bench", "token", transport=httpx.MockTransport(backend))
    with pytest.raises(ValueError, match="HTTP 422") as error:
        await client.request("POST", "/v1/jobs", json={})
    assert "private-credential" not in str(error.value)
    assert len(calls) == 1


async def test_malformed_job_does_not_echo_credentials(caplog):
    server = create_server(ControllerClient("http://bench", "token"))
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool(
            "submit_job",
            {
                "job": {
                    "runtime": "invalid",
                    "secrets": {"IO_KEY": "private-value-do-not-echo"},
                }
            },
        )
        assert result.isError
        assert "private-value-do-not-echo" not in result.model_dump_json()
        assert "private-value-do-not-echo" not in caplog.text


async def test_protocol_preserves_external_credentials_only_in_controller_request():
    received = []

    def backend(request):
        received.append(json.loads(request.content))
        return httpx.Response(202, json={"id": "job-2", "summary": "test-private-key"})

    server = create_server(
        ControllerClient(
            "http://bench",
            "controller-token",
            transport=httpx.MockTransport(backend),
        )
    )
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool(
            "submit_job",
            {
                "job": {
                    "runtime": "wippersnapper-arduino",
                    "device_id": "dut",
                    "broker": "production",
                    "secrets": {"IO_USERNAME": "account", "IO_KEY": "test-private-key"},
                    "expect_serial": "CLOUD_ASSERTION_OK",
                }
            },
        )
        assert not result.isError
        assert received[0]["secrets"]["IO_KEY"] == "test-private-key"
        assert "test-private-key" not in result.model_dump_json()


async def test_secret_redaction_and_image_download():
    client = ControllerClient(
        "http://bench",
        "token",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                content=b"\x89PNG\r\n\x1a\nexample",
                headers={"content-type": "application/octet-stream"},
            )
        ),
    )
    client.secret_values.add("short")
    assert client.sanitize({"IO_KEY": "unknown", "msg": "short aio_abcdef"}) == {
        "IO_KEY": "[redacted]",
        "msg": "[redacted] [redacted]",
    }
    assert (await client.asset("/v1/jobs/job/assets/image/download"))[0].type == "image"


async def test_http_guide_and_openapi_include_public_wifi(authed_client):
    response = await authed_client.get("/v1/job-profiles")
    assert response.status_code == 200
    assert response.json()["wifi"]["ssid"] == "free4all"
    schema = (await authed_client.get("/openapi.json")).json()
    assert (
        "password password"
        in schema["components"]["schemas"]["JobRequest"]["properties"]["secrets"]["description"]
    )


def test_http_requires_token():
    with pytest.raises(ValueError, match="HIL_MCP_TOKEN"):
        http_app(create_server(ControllerClient("http://bench", "token")), "")
