"""WipperSnapper v2 through the real Adafruit IO stack: render jobs, run them, judge them.

Probes the IO web UI's v2 paths on a staging (or dev-lan) IO deployment with a
real bench DUT, using the ``io_*`` firmware-bench stages. Each verdict-producing
stage carries a ``label``; :func:`evaluate` reads the job logs back and classifies
every probe as pass / bug / fixed / fail / infra / prereq / not_run.

    python ws_v2_suite.py render --device-id <id> --firmware-json fw.json --out jobs/
    IO_KEY=... python ws_v2_suite.py run --jobs jobs/ --out results/ --mcp-config cfg.toml
    python ws_v2_suite.py evaluate --results results/

The IO key is never written to a job file: ``run`` passes ``--secret-env IO_KEY``
to ``examples/mcp/run_job.py``, which adds it in memory.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The IO UI's I2C scan timer on staging; a slower reply shows as
#: "Scan timed out" in the wizard even though the device answered.
UI_SCAN_BUDGET_S = 5.0
V1_REGISTERED_MARKER = "Registration and configuration complete"
HARDWARE_STAGES = {"enter_bootloader", "erase", "flash", "verify", "power_cycle"}

_ESP32_FLASH_STAGES: list[dict[str, Any]] = [
    {"type": "enter_bootloader"},
    {"type": "erase", "before": "no_reset", "after": "no_reset"},
    {"type": "flash", "offset": "0x0", "before": "no_reset", "after": "no_reset"},
    {"type": "verify", "before": "no_reset", "after": "no_reset"},
    {"type": "power_cycle"},
]


# --------------------------------------------------------------------------- #
# results model                                                               #
# --------------------------------------------------------------------------- #


@dataclass
class Line:
    tag: str
    fields: dict[str, str]
    raw: str


@dataclass
class Outcome:
    status: str  # pass | bug | fixed | fail | infra | prereq | not_run | inconclusive
    summary: str


@dataclass
class Probe:
    label: str
    question: str
    judge: Callable[[dict[str, list[Line]]], Outcome]


@dataclass
class SuiteJob:
    name: str
    job: dict[str, Any]
    probes: list[Probe] = field(default_factory=list)
    serial_probes: list[Probe] = field(default_factory=list)


_TAGS = (
    "IO_API_VERDICT",
    "IO_WS_PUBLISH",
    "IO_WS_VERDICT",
    "IO_INFRA_ERROR",
    "IO_DEVICE_KEY",
    "SERIAL_ASSERT_VERDICT",
)
_LINE_RE = re.compile(r"\b(" + "|".join(_TAGS) + r")\b(.*)$")
_KV_RE = re.compile(r"(\w+)=(\S+)")


def parse_lines(text: str) -> list[Line]:
    out = []
    for raw in text.splitlines():
        m = _LINE_RE.search(raw)
        if m:
            rest = m.group(2)
            # IO_INFRA_ERROR detail= runs to end of line; keep it whole.
            fields = dict(_KV_RE.findall(rest.split(" detail=", 1)[0]))
            if " detail=" in rest:
                fields["detail"] = rest.split(" detail=", 1)[1]
            out.append(Line(m.group(1), fields, raw.strip()))
    return out


def by_label(lines: Iterable[Line]) -> dict[str, list[Line]]:
    grouped: dict[str, list[Line]] = {}
    for ln in lines:
        grouped.setdefault(ln.fields.get("label", ""), []).append(ln)
    return grouped


def _first(lines: dict[str, list[Line]], label: str, tag: str) -> Line | None:
    return next((ln for ln in lines.get(label, []) if ln.tag == tag), None)


def _infra(lines: dict[str, list[Line]], *labels: str) -> Outcome | None:
    for label in labels:
        hit = _first(lines, label, "IO_INFRA_ERROR")
        if hit:
            return Outcome(
                "infra", f"{label}: {hit.fields.get('kind')} — {hit.fields.get('detail', '')}"
            )
    return None


def _elapsed(ws: Line) -> float:
    try:
        return float(ws.fields.get("elapsed_s", "nan"))
    except ValueError:
        return float("nan")


# --------------------------------------------------------------------------- #
# judges                                                                      #
# --------------------------------------------------------------------------- #


def judge_checkin(
    label: str, *, registered: bool = False
) -> Callable[[dict[str, list[Line]]], Outcome]:
    def judge(lines: dict[str, list[Line]]) -> Outcome:
        if bad := _infra(lines, label):
            return bad
        ws = _first(lines, label, "IO_WS_VERDICT")
        if ws is None:
            return Outcome("not_run", "checkin probe never ran")
        if ws.fields.get("matched") != "true" and registered:
            # The bridge forwards a checkin to the web only for a device that is
            # not yet an IO group; a registered one just has its semver updated.
            return Outcome(
                "info",
                "no ws-b2w checkin, as expected for a registered device (device key given)",
            )
        if ws.fields.get("matched") != "true":
            return Outcome(
                "prereq",
                "no checkin on ws-b2w: firmware/proto mismatch with the bridge, secrets, "
                "or Wi-Fi — not a verdict on the UI",
            )
        topic = ws.fields.get("topic", "")
        if re.search(r"/ws-b2w$", topic):
            return Outcome("pass", f"checkin on bare {topic} — the topic the UI subscribes to")
        # First match only: the bridge may publish to both topics. ui-topic decides.
        return Outcome("inconclusive", f"checkin arrived first on {topic}; see ui-topic")

    return judge


def judge_ui_topic(ui_label: str, other_labels: list[str]) -> Callable[..., Outcome]:
    """Do replies reach the bare ``{user}/ws-b2w`` the UI subscribes to?

    ``ui_label`` subscribes to bare ws-b2w only (the UI's view); the others
    subscribe to both variants and show where replies actually go.
    """

    def judge(lines: dict[str, list[Line]]) -> Outcome:
        ui = _first(lines, ui_label, "IO_WS_VERDICT")
        if ui is not None and ui.fields.get("matched") == "true":
            return Outcome("pass", "replies reach bare ws-b2w, the topic the UI subscribes to")
        elsewhere = [
            ws.fields.get("topic", "")
            for label in other_labels
            if (ws := _first(lines, label, "IO_WS_VERDICT")) and ws.fields.get("matched") == "true"
        ]
        per_device = [t for t in elsewhere if re.search(r"/ws-b2w/[^/]+$", t)]
        if per_device:
            return Outcome(
                "bug",
                f"replies arrive on {per_device[0]} but never on bare ws-b2w, so the UI "
                "(subscribed to bare ws-b2w only) never sees them",
            )
        if ui is None:
            return Outcome("not_run", "topic probe never ran")
        return Outcome("inconclusive", "no probe got a reply on any ws-b2w topic")

    return judge


def judge_probe(label: str) -> Callable[[dict[str, list[Line]]], Outcome]:
    def judge(lines: dict[str, list[Line]]) -> Outcome:
        if bad := _infra(lines, label, f"{label}.pub"):
            return bad
        ws = _first(lines, label, "IO_WS_VERDICT")
        if ws is None:
            return Outcome("not_run", "probe never ran")
        if ws.fields.get("matched") != "true":
            return Outcome("fail", "no matching i2cScanResults on ws-b2w within the budget")
        elapsed = _elapsed(ws)
        if elapsed > UI_SCAN_BUDGET_S:
            return Outcome(
                "bug",
                f"reply took {elapsed:.2f}s — over the UI's {UI_SCAN_BUDGET_S:g}s staging "
                "scan timer, so the wizard shows 'Scan timed out'",
            )
        return Outcome("pass", f"reply in {elapsed:.2f}s (UI budget {UI_SCAN_BUDGET_S:g}s)")

    return judge


def judge_soft_reply(label: str, what: str) -> Callable[[dict[str, list[Line]]], Outcome]:
    def judge(lines: dict[str, list[Line]]) -> Outcome:
        if bad := _infra(lines, label, f"{label}.pub"):
            return bad
        ws = _first(lines, label, "IO_WS_VERDICT")
        if ws is None:
            return Outcome("not_run", "probe never ran")
        if ws.fields.get("matched") == "true":
            return Outcome("pass", f"{what} answered on {ws.fields.get('topic')}")
        return Outcome("inconclusive", f"no {what} reply; the v2 reply shape is unconfirmed")

    return judge


def judge_lifecycle(
    label: str, frame: str, *, rename: bool = False, authoritative: bool = True
) -> Callable[[dict[str, list[Line]]], Outcome]:
    """An API change wrapped in an io_ws_expect for its ws-r2b frame.

    ``authoritative=False`` (the same-pin-info rename): the server may store pin
    info with fields the request lacks, so a 2xx + frame there doesn't prove the
    fix. It reports ``inconclusive`` instead of ``fixed``.
    """

    def judge(lines: dict[str, list[Line]]) -> Outcome:
        if bad := _infra(lines, label, f"{label}.api"):
            return bad
        api = _first(lines, f"{label}.api", "IO_API_VERDICT")
        ws = _first(lines, label, "IO_WS_VERDICT")
        if api is None:
            return Outcome("not_run", "API call never ran (an earlier stage stopped the job)")
        status = int(api.fields.get("status", "0") or 0)
        seen = ws is not None and ws.fields.get("matched") == "true"
        if status == 404:
            return Outcome(
                "prereq",
                "HTTP 404: the device isn't an IO group on this account yet (accept it in "
                "the UI once, or create the group from the checkin)",
            )
        if rename:
            if status >= 500:
                return Outcome("bug", f"rename-only update returned HTTP {status}")
            if 200 <= status < 300 and not seen:
                return Outcome("bug", f"HTTP {status} but no {frame} on ws-r2b")
            if 200 <= status < 300 and seen:
                if not authoritative:
                    return Outcome(
                        "inconclusive",
                        f"HTTP {status} and {frame}, but the stored pin info may differ "
                        "from the request; rely on rename-only",
                    )
                return Outcome("fixed", f"HTTP {status} and {frame} emitted")
            return Outcome("fail", f"unexpected HTTP {status}")
        if not 200 <= status < 300:
            return Outcome("bug" if status >= 500 else "fail", f"HTTP {status}")
        if not seen:
            return Outcome("bug", f"HTTP {status} but no {frame} on ws-r2b")
        return Outcome("pass", f"HTTP {status} and {frame} on {ws.fields.get('topic')}")

    return judge


def judge_serial(
    index: int, ok_msg: str, fail_status: str, fail_msg: str
) -> Callable[..., Outcome]:
    def judge(serial: list[Line]) -> Outcome:
        if len(serial) <= index:
            return Outcome("not_run", "serial assertion never ran")
        if serial[index].fields.get("ok") == "true":
            return Outcome("pass", ok_msg)
        return Outcome(fail_status, fail_msg)

    return judge


# --------------------------------------------------------------------------- #
# rendering                                                                   #
# --------------------------------------------------------------------------- #


@dataclass
class Options:
    io_user: str
    device_id: str
    firmware: dict[str, Any] | None
    sda: int
    scl: int
    address: int
    sensor: str = "pressure"
    #: A second sub-sensor of the same part for the batch (multi-component) probes.
    sensor2: str | None = None
    pin_info: dict[str, Any] | None = None
    flash_stages: list[dict[str, Any]] | None = None
    skip_flash: bool = False
    probes: int = 3
    machine_name: str | None = None
    #: An already-registered device's IO group key. The bridge sends no ws-b2w
    #: checkin for registered devices, so every io_* stage is given the key.
    device_key: str | None = None
    timeout_scale: float = 1.0
    v1_firmware: dict[str, Any] | None = None
    v1_add_marker: str | None = None
    pool: str = "public"
    broker: str = "staging"


def _t(opts: Options, seconds: float) -> float:
    return round(seconds * opts.timeout_scale, 3)


def _preamble(opts: Options) -> list[dict[str, Any]]:
    stages: list[dict[str, Any]] = []
    if not opts.skip_flash:
        stages += deepcopy(opts.flash_stages or _ESP32_FLASH_STAGES)
    else:
        stages.append({"type": "power_cycle"})
    stages.append({"type": "write_secrets_msc"})
    return stages


def feed_key(opts: Options, sensor: str | None = None) -> str:
    # The IO UI's v2 key shape: i2c-{sda}-{scl}(-{mux}-{ch})-0x{addr}-{sensor}.
    return f"i2c-{opts.sda}-{opts.scl}-0x{opts.address:x}-{sensor or opts.sensor}"


def pin_info(opts: Options, sensor: str | None = None, **overrides: Any) -> dict[str, Any]:
    info = (
        deepcopy(opts.pin_info)
        if opts.pin_info
        else {
            "isI2C": True,
            "type": f"bme280:{opts.sensor}",
            "sensorType": opts.sensor,
            "period": 30,
        }
    )
    info["i2cDescriptor"] = {"sda": opts.sda, "scl": opts.scl, "i2cAddress": opts.address}
    if sensor and sensor != opts.sensor:
        # Another sub-sensor of the same part, e.g. vcnl4200:light -> vcnl4200:proximity.
        info["sensorType"] = sensor
        info["type"] = f"{str(info.get('type', '')).split(':')[0]}:{sensor}"
    info.update(overrides)
    return info


def _expect(
    label: str, action: str, timeout: float, step: dict[str, Any] | None = None, **kw: Any
) -> dict[str, Any]:
    stage: dict[str, Any] = {
        "type": "io_ws_expect",
        "label": label,
        "action": action,
        "timeout_s": timeout,
        **kw,
    }
    if step is not None:
        stage["step"] = step
    return stage


def _api(label: str, action: str, **kw: Any) -> dict[str, Any]:
    return {"type": "io_api_component", "label": label, "action": action, "soft": True, **kw}


def _agent_job(
    opts: Options, firmware: dict[str, Any] | None, stages: list[dict[str, Any]], broker: str
) -> dict[str, Any]:
    job: dict[str, Any] = {
        "runtime": "wippersnapper-arduino",
        "device_id": opts.device_id,
        "pool": opts.pool,
        "broker": broker,
        "total_s": 2400,
        "secrets": {"IO_USERNAME": opts.io_user},
        "params": {"stages": stages, "window_minutes": 0},
    }
    if firmware is not None:
        job["firmware"] = deepcopy(firmware)
    return job


def build_v2_job(opts: Options) -> SuiteJob:
    """Checkin topic, probe latency, scan-all and the add/modify/rename/remove lifecycle."""
    if opts.probes < 2:
        raise ValueError("--probes must be at least 2 (probe-1 listens like the UI, others wider)")
    key = feed_key(opts)
    name = f"hil {opts.sensor}"
    stages = _preamble(opts)
    probes: list[Probe] = []

    checkin_kw: dict[str, Any] = {}
    if opts.machine_name:
        checkin_kw["match"] = {"args.machineName": opts.machine_name}
    if opts.device_key:
        # No frame will come for a registered device: the stage is the boot wait.
        checkin_kw.update(soft=True)
    stages.append(
        _expect(
            "checkin",
            "checkin",
            _t(opts, 90 if opts.device_key else 150),
            {"type": "power_cycle"},
            **checkin_kw,
        )
    )
    probes.append(
        Probe(
            "checkin",
            "Which ws-b2w topic carries the checkin?",
            judge_checkin("checkin", registered=bool(opts.device_key)),
        )
    )

    probe_frame = {
        "action": "i2cProbe",
        "targets": [{"sda": opts.sda, "scl": opts.scl}],
        "addresses": [opts.address],
    }
    for i in range(1, opts.probes + 1):
        label = f"probe-{i}"
        stages.append(
            _expect(
                label,
                "i2cScanResults",
                _t(opts, 15),
                {
                    "type": "io_ws_publish",
                    "label": f"{label}.pub",
                    "payload": probe_frame,
                    "errors_window_s": _t(opts, 1.0),
                },
                match_contains={"args.results.0.found": [opts.address]},
                soft=True,
                # probe-1 listens exactly like the UI (bare ws-b2w only); the rest
                # subscribe to both variants to show where replies really go.
                **({"topics": ["ws-b2w"]} if i == 1 else {}),
            )
        )
        when = "first probe after checkin, on the UI's topic" if i == 1 else f"probe {i}"
        probes.append(
            Probe(label, f"Does the {when} answer inside the UI's 5 s budget?", judge_probe(label))
        )
    others = [f"probe-{i}" for i in range(2, opts.probes + 1)]
    probes.append(
        Probe(
            "ui-topic",
            "Do replies reach the bare ws-b2w topic the UI subscribes to?",
            judge_ui_topic("probe-1", others),
        )
    )

    stages.append(
        _expect(
            "scan-all",
            "i2cScanResults",
            _t(opts, 15),
            {
                "type": "io_ws_publish",
                "label": "scan-all.pub",
                "payload": {"action": "i2cScanAll", "sda": opts.sda, "scl": opts.scl},
                "errors_window_s": _t(opts, 1.0),
            },
            soft=True,
        )
    )
    probes.append(
        Probe(
            "scan-all", "Does i2cScanAll get a reply?", judge_soft_reply("scan-all", "i2cScanAll")
        )
    )

    # A previous run that stopped early may have left the feed behind. Later
    # updates/deletes reuse the key the create returns: IO stores feeds in a
    # device group under "{device_key}.{key}", not the bare key sent.
    stages.append(_api("cleanup-before", "delete", feed_key=key, expect_status=[200, 404]))

    lifecycle = [
        (
            "add",
            "create",
            "addComponent",
            {"feed": {"name": name, "key": key, "wipper_pin_info": pin_info(opts)}},
            {},
        ),
        (
            "modify-period",
            "update",
            "modifyComponent",
            {"feed": {"name": name, "wipper_pin_info": pin_info(opts, period=60)}},
            {"match": {"newComponent.period": 60}},
        ),
        (
            "rename-only",
            "update",
            "modifyComponent",
            {"feed": {"name": f"{name} renamed"}},
            {},
        ),
        (
            "rename-same-pin-info",
            "update",
            "modifyComponent",
            {
                "feed": {
                    "name": f"{name} renamed again",
                    "wipper_pin_info": pin_info(opts, period=60),
                },
            },
            {},
        ),
        ("remove", "delete", "removeComponent", {}, {}),
    ]
    questions = {
        "add": "Does a REST create emit addComponent on ws-r2b?",
        "modify-period": "Does a pin-info change emit modifyComponent with the new period?",
        "rename-only": "Does a name-only update succeed and emit modifyComponent?",
        "rename-same-pin-info": (
            "Does an update with unchanged pin info succeed and emit modifyComponent?"
        ),
        "remove": "Does a REST delete emit removeComponent on ws-r2b?",
    }
    for label, action, frame, api_kw, expect_kw in lifecycle:
        stages.append(
            _expect(
                label,
                frame,
                _t(opts, 10),
                _api(f"{label}.api", action, **api_kw),
                soft=True,
                **expect_kw,
            )
        )
        probes.append(
            Probe(
                label,
                questions[label],
                judge_lifecycle(
                    label,
                    frame,
                    rename=label.startswith("rename"),
                    authoritative=label != "rename-same-pin-info",
                ),
            )
        )

    # The UI's multi-sensor (I2C) route: one batch per create / update / destroy,
    # which Rails turns into addMultiComponent / modifyMultiComponent / removeComponent.
    sensors = [opts.sensor] + ([opts.sensor2] if opts.sensor2 else [])

    def batch_feeds(period: int) -> list[dict[str, Any]]:
        return [
            {
                "name": f"{name} {s}",
                "key": feed_key(opts, s),
                "wipper_pin_info": pin_info(opts, sensor=s, period=period),
            }
            for s in sensors
        ]

    batch_steps = [
        (
            "batch-add",
            "addMultiComponent",
            {"create": batch_feeds(30)},
            "Does a batch create emit addMultiComponent on ws-r2b?",
        ),
        (
            "batch-modify",
            "modifyMultiComponent",
            {"update": batch_feeds(60)},
            "Does a batch update emit modifyMultiComponent on ws-r2b?",
        ),
        (
            "batch-remove",
            "removeComponent",
            {"destroy": [feed_key(opts, s) for s in sensors]},
            "Does a batch destroy emit removeComponent on ws-r2b?",
        ),
    ]
    for label, frame, batch, question in batch_steps:
        stages.append(
            _expect(
                label, frame, _t(opts, 10), _api(f"{label}.api", "batch", batch=batch), soft=True
            )
        )
        probes.append(Probe(label, question, judge_lifecycle(label, frame)))

    if opts.device_key:
        for st in walk_io_stages(stages):
            st["device_key"] = opts.device_key
    return SuiteJob("v2-real-io", _agent_job(opts, opts.firmware, stages, opts.broker), probes)


def walk_io_stages(stages: list[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    """Every io_* stage, including ones nested as an io_ws_expect ``step``."""
    for st in stages:
        node: dict[str, Any] | None = st
        while node is not None:
            if str(node.get("type", "")).startswith("io_"):
                yield node
            step = node.get("step")
            node = step if isinstance(step, dict) else None


def build_v1_assisted_job(opts: Options, broker: str) -> SuiteJob:
    """A 1.x DUT checks in; then a person adds a component in the IO UI within 10 minutes."""
    if not opts.v1_add_marker:
        raise ValueError(
            "--v1-add-marker is required: the exact serial line a 1.x build prints "
            "when a component is added (take it from a known-good log)"
        )
    stages = _preamble(opts)
    stages += [
        {"type": "power_cycle"},
        {"type": "assert_serial", "contains": V1_REGISTERED_MARKER, "timeout_s": 180},
        {"type": "serial_checkpoint"},
        {"type": "assert_serial", "contains": opts.v1_add_marker, "timeout_s": 600},
    ]
    serial = [
        Probe(
            "v1-registered",
            "Does the 1.x DUT register on this IO deployment?",
            judge_serial(0, "registered", "prereq", "no registration banner within 180 s"),
        ),
        Probe(
            "v1-ui-add",
            "Does a component added in the IO UI reach a 1.x device?",
            judge_serial(
                1,
                "the UI add reached the device",
                "bug",
                "no add on the device within 600 s — if someone did add a "
                "component in the UI, the UI's v1 component publish is broken",
            ),
        ),
    ]
    return SuiteJob(
        f"v1-ui-assisted-{broker}",
        _agent_job(opts, opts.v1_firmware, stages, broker),
        serial_probes=serial,
    )


def build_suite(
    opts: Options, *, v1: bool = False, v1_brokers: Iterable[str] = ("staging",)
) -> list[SuiteJob]:
    jobs = [build_v2_job(opts)]
    if v1:
        jobs += [build_v1_assisted_job(opts, b) for b in v1_brokers]
    return jobs


def validate(job: dict[str, Any], io_key: str = "aio_RENDERCHECKplaceholder0000") -> dict[str, Any]:
    """Build the controller request the same way MCP prepare_job does (offline)."""
    from hil_controller.job_profiles import AgentJob, build_job

    spec = deepcopy(job)
    spec["secrets"] = {**spec.get("secrets", {}), "IO_KEY": io_key}
    return build_job(AgentJob.model_validate(spec))


# --------------------------------------------------------------------------- #
# evaluation                                                                  #
# --------------------------------------------------------------------------- #


_STAGE_HEADER = "── stage "


def pick_job_log(paths: Iterable[Path]) -> str:
    """The one controller log for a job: the file carrying the ``── stage i/N`` headers.

    Reading every saved asset would count a verdict twice wherever a line is
    echoed into a second log, and the serial judges go by position.
    """
    texts = [p.read_text(encoding="utf-8", errors="replace") for p in paths]
    with_headers = [t for t in texts if _STAGE_HEADER in t]
    if not with_headers:
        return ""
    return max(with_headers, key=lambda t: t.count(_STAGE_HEADER))


def evaluate_job(sjob: SuiteJob, log_text: str) -> list[dict[str, Any]]:
    lines = parse_lines(log_text)
    grouped = by_label(lines)
    serial = [ln for ln in lines if ln.tag == "SERIAL_ASSERT_VERDICT"]
    rows = []
    for probe in sjob.probes:
        out = probe.judge(grouped)
        rows.append(
            {
                "job": sjob.name,
                "probe": probe.label,
                "question": probe.question,
                "status": out.status,
                "summary": out.summary,
            }
        )
    for probe in sjob.serial_probes:
        out = probe.judge(serial)
        rows.append(
            {
                "job": sjob.name,
                "probe": probe.label,
                "question": probe.question,
                "status": out.status,
                "summary": out.summary,
            }
        )
    key = next((ln.fields.get("key") for ln in lines if ln.tag == "IO_DEVICE_KEY"), None)
    if key:
        rows.append(
            {
                "job": sjob.name,
                "probe": "device-key",
                "question": "Captured device key",
                "status": "info",
                "summary": key,
            }
        )
    return rows


def render_markdown(rows: list[dict[str, Any]]) -> str:
    out = ["| job | probe | status | result |", "|---|---|---|---|"]
    for r in rows:
        out.append(f"| {r['job']} | {r['probe']} | **{r['status']}** | {r['summary']} |")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #


def _load_json_arg(value: str | None) -> Any:
    if not value:
        return None
    path = Path(value)
    return json.loads(path.read_text(encoding="utf-8-sig") if path.exists() else value)


def _options(args: argparse.Namespace) -> Options:
    io_user = args.io_user or os.environ.get("IO_USERNAME", "")
    if not io_user:
        raise SystemExit("set --io-user or IO_USERNAME")
    return Options(
        io_user=io_user,
        device_id=args.device_id,
        firmware=_load_json_arg(args.firmware_json),
        sda=args.sda,
        scl=args.scl,
        address=int(str(args.address), 0),
        sensor=args.sensor,
        sensor2=getattr(args, "sensor2", None),
        pin_info=_load_json_arg(args.pin_info_json),
        flash_stages=_load_json_arg(args.flash_stages_json),
        skip_flash=args.skip_flash,
        probes=args.probes,
        machine_name=args.machine_name,
        device_key=getattr(args, "device_key", None),
        v1_firmware=_load_json_arg(args.v1_firmware_json),
        v1_add_marker=args.v1_add_marker,
        pool=args.pool,
    )


def _suite_from_args(args: argparse.Namespace) -> list[SuiteJob]:
    opts = _options(args)
    brokers = ["staging"] + (["production"] if args.v1_production_baseline else [])
    return build_suite(
        opts, v1=bool(args.v1_firmware_json or args.v1_add_marker), v1_brokers=brokers
    )


def _add_render_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--io-user", help="IO username (default: $IO_USERNAME)")
    p.add_argument("--device-id", required=True, help="bench device id from list_targets")
    p.add_argument("--pool", default="public")
    p.add_argument(
        "--firmware-json", help="AgentJob firmware object (JSON or file) for the 2.x build"
    )
    p.add_argument("--flash-stages-json", help="flash stages for non-ESP32 chips (JSON or file)")
    p.add_argument("--skip-flash", action="store_true", help="DUT already runs the build")
    p.add_argument("--sda", type=int, required=True)
    p.add_argument("--scl", type=int, required=True)
    p.add_argument("--address", default="0x76", help="I2C address on the bus (e.g. 0x76)")
    p.add_argument("--sensor", default="pressure")
    p.add_argument("--sensor2", help="second sub-sensor of the same part for the batch probes")
    p.add_argument("--pin-info-json", help="wipper_pin_info copied from a UI-created feed")
    p.add_argument("--probes", type=int, default=3)
    p.add_argument("--machine-name", help="match the checkin's args.machineName (shared account)")
    p.add_argument(
        "--device-key",
        help="IO group key of an already-registered device (it gets no ws-b2w checkin)",
    )
    p.add_argument("--v1-firmware-json", help="1.x firmware for the assisted UI run")
    p.add_argument("--v1-add-marker", help="serial line a 1.x build prints on component add")
    p.add_argument(
        "--v1-production-baseline",
        action="store_true",
        help="also render the assisted v1 job against production (needs your prod key)",
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render", help="write AgentJob files (no IO key) and validate them offline")
    _add_render_args(r)
    r.add_argument("--out", required=True)
    run = sub.add_parser("run", help="submit rendered jobs through examples/mcp/run_job.py")
    run.add_argument("--jobs", required=True)
    run.add_argument("--out", required=True)
    run.add_argument("--mcp-config", required=True)
    run.add_argument("--only", nargs="*", help="job names to run")
    ev = sub.add_parser("evaluate", help="judge results written by run")
    ev.add_argument("--results", required=True)
    args = ap.parse_args(argv)

    if args.cmd == "render":
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        jobs = _suite_from_args(args)
        render_args = {k: v for k, v in vars(args).items() if k not in ("cmd", "out")}
        render_args["io_user"] = jobs[0].job["secrets"]["IO_USERNAME"]
        manifest: dict[str, Any] = {"render_args": render_args, "jobs": []}
        for sj in jobs:
            validate(sj.job)
            (out / f"{sj.name}.json").write_text(json.dumps(sj.job, indent=2), encoding="utf-8")
            manifest["jobs"].append(sj.name)
            print(f"rendered + validated {sj.name}: {len(sj.job['params']['stages'])} stages")
        (out / "suite.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return 0

    if args.cmd == "run":
        if not os.environ.get("IO_KEY"):
            raise SystemExit("export IO_KEY (the account's AIO key) before running")
        jobs_dir, out = Path(args.jobs), Path(args.out)
        manifest = json.loads((jobs_dir / "suite.json").read_text(encoding="utf-8"))
        runner = Path(__file__).resolve().parents[1] / "mcp" / "run_job.py"
        for name in manifest["jobs"]:
            if args.only and name not in args.only:
                continue
            print(f"== {name}", flush=True)
            subprocess.run(
                [
                    sys.executable,
                    str(runner),
                    "--job",
                    str(jobs_dir / f"{name}.json"),
                    "--output",
                    str(out / name),
                    "--mcp-config",
                    args.mcp_config,
                    "--secret-env",
                    "IO_KEY",
                ],
                check=False,
            )
        (out / "suite.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return 0

    results = Path(args.results)
    manifest = json.loads((results / "suite.json").read_text(encoding="utf-8"))
    ns = argparse.Namespace(**manifest["render_args"])
    rows: list[dict[str, Any]] = []
    for sj in _suite_from_args(ns):
        if sj.name not in manifest["jobs"]:
            continue
        rows += evaluate_job(sj, pick_job_log(sorted((results / sj.name).glob("*.txt"))))
    (results / "verdicts.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    md = render_markdown(rows)
    (results / "verdicts.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
