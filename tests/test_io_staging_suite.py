"""Offline checks for examples/io-staging-ws-v2/ws_v2_suite.py.

Every rendered job goes through the real job-profile builder, and the v2 job's
io_* stages run against the in-memory broker + MockTransport from
test_io_stack_stages, once behaving like the IO stack with the known defects
and once with them fixed, so each verdict path is exercised. No live network.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

import hil_controller.adapters.bench_stages as bs
from hil_controller.adapters.bench_stages import run_stages, walk_stages

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "io-staging-ws-v2"))
import ws_v2_suite as suite  # noqa: E402

from tests.test_io_stack_stages import DEVICE, IO_KEY, USER, FakeBroker, _ctx  # noqa: E402


def _opts(**kw: Any) -> suite.Options:
    base: dict[str, Any] = {
        "io_user": USER,
        "device_id": "mcu-qtpy-esp32s3-example",
        "firmware": {"path": "/tmp/ws2.combined.bin", "offset": "0x0"},
        "sda": 41,
        "scl": 40,
        "address": 0x76,
        "timeout_scale": 0.05,
        "v1_firmware": {"path": "/tmp/ws1.combined.bin", "offset": "0x0"},
        "v1_add_marker": "Added component",
    }
    base.update(kw)
    return suite.Options(**base)


# --------------------------------------------------------------------------- #
# rendering                                                                   #
# --------------------------------------------------------------------------- #


def test_every_rendered_job_builds_through_the_job_profile() -> None:
    jobs = suite.build_suite(_opts(), v1=True, v1_brokers=("staging", "production"))
    assert [j.name for j in jobs] == [
        "v2-real-io",
        "v1-ui-assisted-staging",
        "v1-ui-assisted-production",
    ]
    for sj in jobs:
        assert "IO_KEY" not in sj.job["secrets"]  # the key is never rendered to disk
        request = suite.validate(sj.job)
        stages = list(walk_stages(request["params"]["stages"]))
        host = "io.adafruit.com" if sj.name.endswith("production") else "io.adafruit.us"
        for st in stages:
            if st["type"].startswith("io_"):
                assert st["io_host"] == "io.adafruit.us" and st["io_port"] == 8883
                assert st["label"]
            if st["type"] == "write_secrets_msc":
                assert st["io_url"] == host


def test_v2_job_is_refused_for_production() -> None:
    sj = suite.build_v2_job(_opts(broker="production"))
    with pytest.raises(ValueError, match="allowed only for broker=staging or dev-lan"):
        suite.validate(sj.job)


def test_v1_assisted_job_needs_the_add_marker() -> None:
    with pytest.raises(ValueError, match="--v1-add-marker"):
        suite.build_v1_assisted_job(_opts(v1_add_marker=None), "staging")


def test_labels_are_unique_and_every_probe_has_stages() -> None:
    sj = suite.build_v2_job(_opts(probes=3))
    labels = [s["label"] for s in walk_stages(sj.job["params"]["stages"]) if "label" in s]
    assert len(labels) == len(set(labels))
    # ui-topic is derived from probe-1 (bare ws-b2w only) vs the wider probes
    assert {p.label for p in sj.probes} - {"ui-topic"} <= set(labels)


# --------------------------------------------------------------------------- #
# simulated IO stack                                                          #
# --------------------------------------------------------------------------- #


def _simulate(
    monkeypatch: pytest.MonkeyPatch, *, defects: bool
) -> tuple[suite.SuiteJob, list[dict[str, Any]]]:
    broker = FakeBroker()
    monkeypatch.setattr(bs, "_io_mqtt_client", broker.factory)
    r2b = f"{USER}/ws-r2b/{DEVICE}"
    b2w = f"{USER}/ws-b2w/{DEVICE}" if defects else f"{USER}/ws-b2w"

    async def fake_power_cycle(stage: dict[str, Any], ctx: Any) -> None:
        broker.deliver(
            b2w,
            {
                "deviceId": DEVICE,
                "action": "checkin",
                "args": {"machineName": "qtpy-esp32s3", "deviceId": DEVICE},
            },
        )

    monkeypatch.setitem(bs.STAGE_HANDLERS, "power_cycle", fake_power_cycle)
    scan = {
        "deviceId": DEVICE,
        "action": "i2cScanResults",
        "args": {"results": [{"sda": 41, "scl": 40, "found": [0x76]}]},
    }
    broker.replies[f"{USER}/ws-w2b/{DEVICE}"] = [(b2w, scan)]

    feeds: dict[str, dict[str, Any]] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        key = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content) if req.content else {}
        if req.method == "POST":
            feed = body["feed"]
            feeds[feed["key"]] = feed
            broker.deliver(r2b, {"action": "addComponent", "component": feed["wipper_pin_info"]})
            return httpx.Response(201, json={"key": feed["key"]})
        if key not in feeds:
            return httpx.Response(404, json={"error": "not found"})
        if req.method == "DELETE":
            old = feeds.pop(key)
            broker.deliver(r2b, {"action": "removeComponent", "component": old["wipper_pin_info"]})
            return httpx.Response(200, json={})
        new = body["feed"]
        old_info = feeds[key]["wipper_pin_info"]
        new_info = new.get("wipper_pin_info", old_info)
        if new_info == old_info and defects:
            return httpx.Response(500, text="Internal Server Error")
        feeds[key] = {**feeds[key], **new, "wipper_pin_info": new_info}
        broker.deliver(
            r2b, {"action": "modifyComponent", "oldComponent": old_info, "newComponent": new_info}
        )
        return httpx.Response(200, json={"key": key})

    def factory(api_base: str, io_key: str) -> httpx.AsyncClient:
        assert io_key == IO_KEY
        return httpx.AsyncClient(base_url=api_base, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(bs, "_io_http_client", factory)

    sj = suite.build_v2_job(_opts(probes=2))
    stages = [
        s
        for s in suite.validate(sj.job)["params"]["stages"]
        if s["type"] not in suite.HARDWARE_STAGES | {"write_secrets_msc"}
    ]
    return sj, stages


async def _run(monkeypatch: pytest.MonkeyPatch, *, defects: bool) -> dict[str, dict[str, Any]]:
    sj, stages = _simulate(monkeypatch, defects=defects)
    ctx, lines = _ctx()
    await run_stages(stages, ctx)
    assert not any(IO_KEY in ln for ln in lines)
    rows = suite.evaluate_job(sj, "\n".join(lines))
    return {r["probe"]: r for r in rows}


async def test_suite_flags_the_known_defects(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = await _run(monkeypatch, defects=True)
    status = {k: v["status"] for k, v in rows.items()}
    assert status == {
        "checkin": "inconclusive",  # first match was the per-device topic
        "probe-1": "fail",  # nothing on bare ws-b2w, where the UI listens
        "probe-2": "pass",
        "ui-topic": "bug",
        "scan-all": "pass",
        "add": "pass",
        "modify-period": "pass",
        "rename-only": "bug",
        "rename-same-pin-info": "bug",
        "remove": "pass",
        "device-key": "info",
    }
    assert "HTTP 500" in rows["rename-only"]["summary"]
    assert rows["device-key"]["summary"] == DEVICE


async def test_suite_reports_fixed_when_the_stack_behaves(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = await _run(monkeypatch, defects=False)
    assert rows["checkin"]["status"] == "pass"
    assert rows["probe-1"]["status"] == "pass"
    assert rows["ui-topic"]["status"] == "pass"
    assert rows["rename-only"]["status"] == "fixed"
    # stored pin info may differ from the request, so this one can't prove the fix
    assert rows["rename-same-pin-info"]["status"] == "inconclusive"
    assert rows["remove"]["status"] == "pass"


# --------------------------------------------------------------------------- #
# judges on synthetic logs                                                    #
# --------------------------------------------------------------------------- #


def _rows(text: str, **opts: Any) -> dict[str, dict[str, Any]]:
    sj = suite.build_v2_job(_opts(**opts))
    return {r["probe"]: r for r in suite.evaluate_job(sj, text)}


def test_slow_probe_is_a_bug_against_the_ui_budget() -> None:
    text = (
        "12:00:01 IO_WS_VERDICT label=checkin matched=true action=checkin "
        f"topic={USER}/ws-b2w elapsed_s=9.10\n"
        "12:00:09 IO_WS_VERDICT label=probe-1 matched=true action=i2cScanResults "
        f"topic={USER}/ws-b2w elapsed_s=6.20\n"
        "IO_WS_VERDICT label=probe-2 matched=true action=i2cScanResults "
        f"topic={USER}/ws-b2w elapsed_s=0.80\n"
    )
    rows = _rows(text, probes=2)
    assert rows["probe-1"]["status"] == "bug" and "6.20s" in rows["probe-1"]["summary"]
    assert rows["probe-2"]["status"] == "pass"
    assert rows["add"]["status"] == "not_run"


def test_infra_prereq_and_not_run_are_not_verdicts() -> None:
    text = (
        "IO_WS_VERDICT label=checkin matched=true action=checkin "
        f"topic={USER}/ws-b2w elapsed_s=30\n"
        "IO_INFRA_ERROR stage=io_ws_publish label=probe-1.pub kind=acl detail=broker reported "
        f"on {USER}/errors: not authorized\n"
        "IO_API_VERDICT label=add.api status=404 key=k action=create ok=false\n"
        "IO_WS_VERDICT label=add matched=false action=addComponent topic=x "
        "frames_seen=0 elapsed_s=10\n"
    )
    rows = _rows(text, probes=2)
    assert rows["probe-1"]["status"] == "infra" and "acl" in rows["probe-1"]["summary"]
    assert rows["add"]["status"] == "prereq"
    assert rows["remove"]["status"] == "not_run"


def test_missing_checkin_is_a_prerequisite_failure() -> None:
    rows = _rows(
        "IO_WS_VERDICT label=checkin matched=false action=checkin topic=a,b "
        "frames_seen=0 elapsed_s=150\n"
    )
    assert rows["checkin"]["status"] == "prereq"


def test_v1_assisted_serial_judges() -> None:
    sj = suite.build_v1_assisted_job(_opts(), "staging")
    rows = {
        r["probe"]: r["status"]
        for r in suite.evaluate_job(
            sj, "SERIAL_ASSERT_VERDICT ok=true\nSERIAL_ASSERT_VERDICT ok=false\n"
        )
    }
    assert rows == {"v1-registered": "pass", "v1-ui-add": "bug"}


def test_only_the_controller_log_is_judged(tmp_path: Path) -> None:
    job_log = "\n".join(
        [
            "── stage 1/3: power_cycle ──",
            "── stage 2/3: assert_serial ──",
            "SERIAL_ASSERT_VERDICT ok=true",
            "── stage 3/3: assert_serial ──",
            "SERIAL_ASSERT_VERDICT ok=false",
        ]
    )
    (tmp_path / "a-0.txt").write_text("SERIAL_ASSERT_VERDICT ok=true", encoding="utf-8")
    (tmp_path / "b-0.txt").write_text(job_log, encoding="utf-8")
    text = suite.pick_job_log(sorted(tmp_path.glob("*.txt")))
    sj = suite.build_v1_assisted_job(_opts(), "staging")
    rows = {r["probe"]: r["status"] for r in suite.evaluate_job(sj, text)}
    # an echoed ok=true in another asset must not shift the add verdict to "pass"
    assert rows == {"v1-registered": "pass", "v1-ui-add": "bug"}


def test_probe_count_below_two_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 2"):
        suite.build_v2_job(_opts(probes=1))
