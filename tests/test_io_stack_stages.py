"""Tests for the real-IO-stack stages (io_api_component / io_ws_publish / io_ws_expect).

No live network: the REST client is an ``httpx.MockTransport`` and MQTT is a
tiny in-memory broker standing in for aiomqtt, both swapped in through the
module's client-factory seams.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

import hil_controller.adapters.bench_stages as bs
from hil_controller.adapters.bench_stages import (
    BenchContext,
    StageError,
    run_stages,
    validate_stages,
)
from hil_controller.job_profiles import AgentJob, build_job

USER = "bench_demo"
IO_KEY = "aio_EXAMPLEonlyNotARealKey0000"
DEVICE = "io-wipper-esp32s3abcdef"
FEED = "i2c-41-40-0x76-bme280-pressure"
HOST = "io.adafruit.us"


def _ctx(**kw: Any) -> tuple[BenchContext, list[str]]:
    lines: list[str] = []
    ctx = BenchContext(
        dut_transport=AsyncMock(),
        hub_transport=AsyncMock(),
        flash_serial_port="/dev/null",
        secrets={"IO_USERNAME": USER, "IO_KEY": IO_KEY, "WIFI_SSID": "free4all"},
        emit=lines.append,
        **kw,
    )
    return ctx, lines


def _stage(kind: str, **kw: Any) -> dict[str, Any]:
    return {"type": kind, "io_host": HOST, "io_port": 8883, **kw}


def _no_key_leak(lines: list[str], *extra: str) -> None:
    for text in [*lines, *extra]:
        assert IO_KEY not in text


# --------------------------------------------------------------------------- #
# fakes                                                                       #
# --------------------------------------------------------------------------- #


def _topic_matches(sub: str, topic: str) -> bool:
    if sub.endswith("/#"):
        return topic == sub[:-2] or topic.startswith(sub[:-1])
    return sub == topic


class _Msg:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


class FakeBroker:
    """In-memory MQTT broker: routes publishes to every matching subscriber."""

    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []
        self.clients: list[FakeClient] = []
        self.denied: set[str] = set()
        self.connect_error: Exception | None = None
        #: ws-w2b publish → frames the "bridge" answers with: [(topic, obj), ...]
        self.replies: dict[str, list[tuple[str, Any]]] = {}
        self.tls: list[bool] = []

    def deliver(self, topic: str, obj: Any) -> None:
        payload = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        for c in self.clients:
            if any(_topic_matches(s, topic) for s in c.subs):
                c.queue.put_nowait(_Msg(topic, payload))

    def factory(self, target: Any, *, tls: bool) -> FakeClient:
        self.tls.append(tls)
        assert target.username == USER and target.key == IO_KEY
        return FakeClient(self)


class FakeClient:
    def __init__(self, broker: FakeBroker) -> None:
        self.broker = broker
        self.subs: list[str] = []
        self.queue: asyncio.Queue[_Msg] = asyncio.Queue()

    async def __aenter__(self) -> FakeClient:
        if self.broker.connect_error:
            raise self.broker.connect_error
        self.broker.clients.append(self)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.broker.clients.remove(self)

    async def subscribe(self, topic: str, qos: int = 0) -> list[int]:
        self.broker.events.append(("sub", topic))
        if topic in self.broker.denied:
            return [0x80]
        self.subs.append(topic)
        return [qos]

    async def publish(self, topic: str, payload: bytes, qos: int = 0) -> None:
        self.broker.events.append(("pub", topic, json.loads(payload)))
        for rtopic, obj in self.broker.replies.get(topic, []):
            self.broker.deliver(rtopic, obj)

    @property
    def messages(self) -> Any:
        async def gen() -> Any:
            while True:
                yield await self.queue.get()

        return gen()


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> FakeBroker:
    b = FakeBroker()
    monkeypatch.setattr(bs, "_io_mqtt_client", b.factory)
    return b


def _http(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def factory(api_base: str, io_key: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=api_base,
            headers={"X-AIO-Key": io_key},
            transport=httpx.MockTransport(wrapped),
        )

    monkeypatch.setattr(bs, "_io_http_client", factory)
    return seen


# --------------------------------------------------------------------------- #
# io_api_component                                                            #
# --------------------------------------------------------------------------- #

PIN_INFO = {
    "isI2C": True,
    "type": "bme280:pressure",
    "period": 30,
    "i2cDescriptor": {"sda": 41, "scl": 40, "i2cAddress": 118},
}


async def test_api_create_update_delete_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(201, json={"key": FEED, "name": "bme280 pressure"})
        if req.method == "PUT":
            return httpx.Response(200, json={"key": FEED})
        return httpx.Response(200, json={})

    seen = _http(monkeypatch, handler)
    ctx, lines = _ctx(io_device_key=DEVICE)
    feed = {"name": "bme280 pressure", "wipper_pin_info": PIN_INFO}
    await run_stages(
        [
            _stage("io_api_component", feed=feed),
            _stage("io_api_component", action="update", feed={"name": "renamed"}),
            _stage("io_api_component", action="delete"),
        ],
        ctx,
    )
    base = f"https://{HOST}/api/v2/{USER}/groups/{DEVICE}/feeds"
    assert [(r.method, str(r.url)) for r in seen] == [
        ("POST", base),
        ("PUT", f"{base}/{FEED}"),
        ("DELETE", f"{base}/{FEED}"),
    ]
    assert all(r.headers["X-AIO-Key"] == IO_KEY for r in seen)
    assert json.loads(seen[0].content) == {"feed": feed}
    assert f"IO_API_VERDICT status=201 key={FEED} action=create ok=true" in lines
    assert f"IO_API_VERDICT status=200 key={FEED} action=delete ok=true" in lines
    assert ctx.io_feed_key == ""  # cleared by the delete
    _no_key_leak(lines, ctx.transcript_text())


async def test_api_auth_failure_is_infra_not_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    _http(monkeypatch, lambda r: httpx.Response(401, text=f"bad key {IO_KEY}"))
    ctx, lines = _ctx()
    with pytest.raises(StageError, match="infra error \\(auth\\)") as exc:
        await run_stages(
            [_stage("io_api_component", device_key=DEVICE, feed={"name": "x"})], ctx
        )
    assert any(ln.startswith("IO_INFRA_ERROR stage=io_api_component kind=auth") for ln in lines)
    assert not any("IO_API_VERDICT" in ln for ln in lines)
    _no_key_leak(lines, str(exc.value))


async def test_api_network_failure_is_infra(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=req)

    _http(monkeypatch, boom)
    ctx, lines = _ctx(io_device_key=DEVICE)
    with pytest.raises(StageError, match="network"):
        await run_stages([_stage("io_api_component", feed={"name": "x"})], ctx)
    assert not any("IO_API_VERDICT" in ln for ln in lines)


async def test_api_server_error_is_a_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    _http(monkeypatch, lambda r: httpx.Response(500, json={"error": "NoMethodError"}))
    ctx, lines = _ctx(io_device_key=DEVICE, io_feed_key=FEED)
    stage = _stage("io_api_component", action="update", feed={"name": "rename only"})
    with pytest.raises(StageError, match="HTTP 500"):
        await run_stages([stage], ctx)
    assert f"IO_API_VERDICT status=500 key={FEED} action=update ok=false" in lines
    # soft: verdict logged, job carries on; expect_status can also flip the sense.
    await run_stages([{**stage, "soft": True}], ctx)
    await run_stages([{**stage, "expect_status": [500]}], ctx)
    assert f"IO_API_VERDICT status=500 key={FEED} action=update ok=true" in lines


@pytest.mark.parametrize(
    "stage,match",
    [
        ({"io_host": "io.adafruit.com"}, "refusing production"),
        ({"io_host": "mqtt.io.adafruit.com"}, "refusing production"),
        ({"io_host": ""}, "needs io_host"),
        ({"io_host": "evil.example/path"}, "needs io_host"),
        ({"io_key": "x"}, "job secrets"),
        ({"io_api_base": "https://attacker.example"}, "io_api_base"),
        ({"device_key": "../../users"}, "invalid device_key"),
        ({"action": "update"}, "feed_key"),
        ({"action": "explode"}, "action must be"),
        ({"feed": None}, "needs a 'feed'"),
    ],
)
async def test_api_rejects_bad_config(stage: dict[str, Any], match: str) -> None:
    ctx, _ = _ctx(io_device_key=DEVICE)
    with pytest.raises(StageError, match=match):
        await run_stages([{**_stage("io_api_component", feed={"name": "x"}), **stage}], ctx)


async def test_api_requires_device_key_and_secrets() -> None:
    ctx, _ = _ctx()
    with pytest.raises(StageError, match="no device key"):
        await run_stages([_stage("io_api_component", feed={"name": "x"})], ctx)
    ctx.secrets = {"IO_USERNAME": USER}
    with pytest.raises(StageError, match="IO_USERNAME and IO_KEY"):
        await run_stages([_stage("io_api_component", device_key=DEVICE)], ctx)


# --------------------------------------------------------------------------- #
# io_ws_publish / io_ws_expect                                                #
# --------------------------------------------------------------------------- #

PROBE = {"action": "i2cProbe", "targets": [{"sda": 41, "scl": 40}], "addresses": [118]}
SCAN_RESULT = {
    "deviceId": DEVICE,
    "action": "i2cScanResults",
    "args": {"results": [{"sda": 41, "scl": 40, "found": [118, 119]}]},
}


async def test_expect_subscribes_before_guarded_publish(broker: FakeBroker) -> None:
    broker.replies[f"{USER}/ws-w2b/{DEVICE}"] = [(f"{USER}/ws-b2w", SCAN_RESULT)]
    ctx, lines = _ctx(io_device_key=DEVICE)
    await run_stages(
        [
            _stage(
                "io_ws_expect",
                action="i2cScanResults",
                match_contains={"args.results.0.found": [118]},
                step=_stage("io_ws_publish", payload=PROBE),
            )
        ],
        ctx,
    )
    kinds = [e[:2] for e in broker.events]
    assert kinds.index(("sub", f"{USER}/ws-b2w")) < kinds.index(
        ("pub", f"{USER}/ws-w2b/{DEVICE}")
    )
    assert ("sub", f"{USER}/ws-b2w/#") in kinds
    assert ("pub", f"{USER}/ws-w2b/{DEVICE}", PROBE) in broker.events
    assert broker.tls == [True, True]
    assert "IO_WS_VERDICT matched=true action=i2cScanResults topic=bench_demo/ws-b2w" in lines
    assert any(ln.startswith("IO_WS_PUBLISH published=true") for ln in lines)
    _no_key_leak(lines)


async def test_expect_ignores_other_devices_and_times_out(broker: FakeBroker) -> None:
    other = {**SCAN_RESULT, "deviceId": "io-wipper-someoneelse"}
    broker.replies[f"{USER}/ws-w2b/{DEVICE}"] = [(f"{USER}/ws-b2w", other)]
    ctx, lines = _ctx(io_device_key=DEVICE)
    stage = _stage(
        "io_ws_expect",
        action="i2cScanResults",
        timeout_s=0.2,
        step=_stage("io_ws_publish", payload=PROBE),
    )
    with pytest.raises(StageError, match="no i2cScanResults frame"):
        await run_stages([stage], ctx)
    assert any(
        ln.startswith("IO_WS_VERDICT matched=false action=i2cScanResults") and "frames_seen=1" in ln
        for ln in lines
    )
    await run_stages([{**stage, "soft": True}], ctx)  # soft: verdict only


async def test_expect_checkin_captures_device_key(
    broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkin = {
        "deviceId": DEVICE,
        "action": "checkin",
        "args": {"machineName": "esp32s3", "deviceId": DEVICE, "semver": "2.0.0-beta.1"},
    }

    async def fake_power_cycle(stage: dict[str, Any], ctx: BenchContext) -> None:
        broker.deliver(f"{USER}/ws-b2w", checkin)

    monkeypatch.setitem(bs.STAGE_HANDLERS, "power_cycle", fake_power_cycle)
    ctx, lines = _ctx()
    await run_stages(
        [
            _stage(
                "io_ws_expect",
                action="checkin",
                match={"args.semver": "2.0.0-beta.1"},
                timeout_s=1,
                step={"type": "power_cycle"},
            )
        ],
        ctx,
    )
    assert ctx.io_device_key == DEVICE
    assert f"IO_DEVICE_KEY key={DEVICE} (from checkin deviceId)" in lines


async def test_expect_r2b_component_add_from_api(
    broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    def rails(req: httpx.Request) -> httpx.Response:
        broker.deliver(
            f"{USER}/ws-r2b/{DEVICE}",
            {"action": "addComponent", "component": {"wipper_pin_info": PIN_INFO}},
        )
        return httpx.Response(201, json={"key": FEED})

    _http(monkeypatch, rails)
    ctx, lines = _ctx(io_device_key=DEVICE)
    await run_stages(
        [
            _stage(
                "io_ws_expect",
                action="addComponent",
                match={"component.wipper_pin_info.i2cDescriptor.i2cAddress": 118},
                step=_stage("io_api_component", feed={"name": "p", "wipper_pin_info": PIN_INFO}),
            )
        ],
        ctx,
    )
    assert ("sub", f"{USER}/ws-r2b/{DEVICE}") in broker.events  # default r2b topic
    assert f"IO_WS_VERDICT matched=true action=addComponent topic={USER}/ws-r2b/{DEVICE}" in lines
    assert ctx.io_feed_key == FEED


async def test_expect_suback_denied_is_infra(broker: FakeBroker) -> None:
    broker.denied.add(f"{USER}/ws-r2b/#")
    ctx, lines = _ctx()
    with pytest.raises(StageError, match="infra error \\(acl\\)"):
        await run_stages([_stage("io_ws_expect", action="addComponent")], ctx)
    assert not any("IO_WS_VERDICT" in ln for ln in lines)


async def test_broker_errors_topic_is_infra(broker: FakeBroker) -> None:
    topic = f"{USER}/ws-w2b/{DEVICE}"
    broker.replies[topic] = [(f"{USER}/errors", f"not authorized to publish {topic}".encode())]
    ctx, lines = _ctx(io_device_key=DEVICE)
    with pytest.raises(StageError, match="infra error \\(acl\\)"):
        await run_stages([_stage("io_ws_publish", payload=PROBE)], ctx)
    assert any("IO_INFRA_ERROR stage=io_ws_publish kind=acl" in ln for ln in lines)


async def test_connect_refused_is_auth_infra(broker: FakeBroker) -> None:
    from aiomqtt.exceptions import MqttConnectError

    broker.connect_error = MqttConnectError(5)
    ctx, lines = _ctx(io_device_key=DEVICE)
    with pytest.raises(StageError, match="infra error \\(auth\\)") as exc:
        await run_stages([_stage("io_ws_expect", action="checkin")], ctx)
    assert not any("IO_WS_VERDICT" in ln for ln in lines)
    _no_key_leak(lines, str(exc.value))


async def test_guarded_step_failure_propagates_unchanged(
    broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing(stage: dict[str, Any], ctx: BenchContext) -> None:
        raise StageError("power_cycle: solenoid hub unreachable")

    monkeypatch.setitem(bs.STAGE_HANDLERS, "power_cycle", failing)
    ctx, lines = _ctx()
    with pytest.raises(StageError, match="solenoid hub unreachable"):
        await run_stages(
            [_stage("io_ws_expect", action="checkin", step={"type": "power_cycle"})], ctx
        )
    assert not any("IO_INFRA_ERROR" in ln for ln in lines)


async def test_plain_mqtt_port_disables_tls(broker: FakeBroker) -> None:
    ctx, _ = _ctx(io_device_key=DEVICE)
    stage = {**_stage("io_ws_publish", payload=PROBE), "io_host": "dev.local", "io_port": 1883}
    await run_stages([stage], ctx)
    assert broker.tls == [False]


@pytest.mark.parametrize(
    "stage,match",
    [
        ({"topics": ["wprsnpr/#"]}, "topics must be"),
        ({"topics": ["ws-b2w/+"]}, "topics must be"),
        ({"topics": ["ws-r2b/../x"]}, "topics must be"),
    ],
)
async def test_expect_rejects_bad_topics(stage: dict[str, Any], match: str) -> None:
    ctx, _ = _ctx()
    with pytest.raises(StageError, match=match):
        await run_stages([{**_stage("io_ws_expect", action="checkin"), **stage}], ctx)


async def test_publish_requires_action_payload(broker: FakeBroker) -> None:
    ctx, _ = _ctx(io_device_key=DEVICE)
    with pytest.raises(StageError, match="payload must be"):
        await run_stages([_stage("io_ws_publish", payload={"sda": 41})], ctx)


def test_validate_stages_walks_nested_step() -> None:
    validate_stages([{"type": "io_ws_expect", "step": {"type": "io_ws_publish"}}])
    with pytest.raises(StageError, match="unknown bench stage type"):
        validate_stages([{"type": "io_ws_expect", "step": {"type": "frobnicate"}}])
    with pytest.raises(StageError, match="only valid as an object on io_ws_expect"):
        validate_stages([{"type": "power_cycle", "step": {"type": "erase"}}])


# --------------------------------------------------------------------------- #
# job_profiles gating                                                         #
# --------------------------------------------------------------------------- #

IO_STAGES = [
    {"type": "power_cycle"},
    {"type": "write_secrets_msc"},
    {
        "type": "io_ws_expect",
        "action": "checkin",
        "timeout_s": 120,
        "step": {"type": "power_cycle"},
    },
    {
        "type": "io_ws_expect",
        "action": "i2cScanResults",
        "step": {"type": "io_ws_publish", "payload": PROBE},
    },
]


def _spec(broker: str, stages: list[dict[str, Any]], **kw: Any) -> AgentJob:
    extra = {"broker_host": "dev.local", "broker_port": 1883} if broker == "dev-lan" else {}
    return AgentJob.model_validate(
        {
            "runtime": "wippersnapper-arduino",
            "device_id": "dut",
            "broker": broker,
            "secrets": {"IO_USERNAME": USER, "IO_KEY": IO_KEY},
            "params": {"stages": stages},
            **extra,
            **kw,
        }
    )


@pytest.mark.parametrize(
    "broker,host,port", [("staging", HOST, 8883), ("dev-lan", "dev.local", 1883)]
)
def test_profile_stamps_io_host_on_nested_stages(broker: str, host: str, port: int) -> None:
    stages = build_job(_spec(broker, IO_STAGES))["params"]["stages"]
    publish = stages[3]["step"]
    assert (stages[2]["io_host"], stages[2]["io_port"]) == (host, port)
    assert (publish["io_host"], publish["io_port"]) == (host, port)
    assert stages[1]["io_url"] == host


@pytest.mark.parametrize("broker", ["production", "protomq"])
def test_profile_rejects_io_stages_off_staging(broker: str) -> None:
    kw = {"secrets": {}} if broker == "protomq" else {}
    with pytest.raises(ValueError, match="only for broker=staging or dev-lan"):
        build_job(_spec(broker, [{"type": "io_ws_publish", "payload": PROBE}], **kw))
    # ...including when wrapped as a nested step.
    nested = [{"type": "io_ws_expect", "step": {"type": "power_cycle"}}]
    with pytest.raises(ValueError, match="only for broker=staging or dev-lan"):
        build_job(_spec(broker, nested, **kw))


@pytest.mark.parametrize(
    "stages",
    [
        [{"type": "io_ws_expect", "step": {"type": "inject_protobuf", "payload_hex": "00"}}],
        [{"type": "io_ws_expect", "step": {"type": "verify_checkin"}}],
        [{"type": "io_ws_publish", "io_host": "attacker.example", "payload": PROBE}],
        [{"type": "io_ws_expect", "step": {"type": "io_api_component", "io_key": "x"}}],
        [{"type": "io_ws_expect", "step": {"type": "write_secrets_msc", "io_url": "x"}}],
    ],
)
def test_profile_rejects_smuggled_or_rerouted_stages(stages: list[dict[str, Any]]) -> None:
    with pytest.raises(ValueError):
        build_job(_spec("staging", stages))
