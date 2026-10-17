"""Operator pages that mirror the agent surface (JSON API + MCP server + skills).

An operator gets what an agent gets, without leaving the web UI:

- ``/ui/jobs/resolve``  — "which device would this job get?" dry-run: the scheduler's
  matcher (the one ``get_adapter`` runs) plus ``POST /v1/topology/resolve``.
- ``/ui/jobs/profiles`` — ``GET /v1/job-profiles`` / MCP ``job_guide`` as cards, with
  recipes that pre-fill the raw submit page.
- ``/ui/jobs/raw``      — raw JSON submit (``JobRequest`` or MCP ``AgentJob``), with a
  redacted preview of the exact request and the in-process ``POST /v1/jobs`` path.
- ``/ui/skills``        — the skills the MCP server serves (``SkillStore``), read-only.
- ``/ui/audit``         — the ``audit_log`` table, filterable and paginated.

Nothing here renders a secret: every request echo goes through :func:`redact`, which
applies :mod:`hil_controller.redact`'s ``****last4`` masking.
"""

from __future__ import annotations

import html
import json
import math
import os
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Cookie, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from hil_controller.auth.principal import Principal
from hil_controller.db.connection import get_db
from hil_controller.redact import mask_secret, mask_values
from hil_controller.web.router import _check_web_token, _login_redirect, _tr

router = APIRouter(prefix="/ui", tags=["web"])

#: Sub-navigation shared by the ops pages (base.html's nav is owned elsewhere).
OPS_NAV = [
    ("resolve", "/ui/jobs/resolve", "Resolve target"),
    ("profiles", "/ui/jobs/profiles", "Job profiles"),
    ("raw", "/ui/jobs/raw", "Raw JSON job"),
    ("skills", "/ui/skills", "Skills"),
    ("audit", "/ui/audit", "Audit log"),
]


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

#: Dict keys whose values are secrets (matches the MCP sanitizer's intent).
_SENSITIVE_KEY = re.compile(
    r"(^|[_-])(key|password|passwd|token|secret|secrets|pat|authorization|credentials?)$",
    re.IGNORECASE,
)
#: Identifier-shaped ``*_key`` fields used by the io_* stages; not credentials.
_NOT_SECRET_KEYS = frozenset({"device_key", "feed_key"})
_AIO_KEY = re.compile(r"aio_[A-Za-z0-9_-]{4,}")
_URL_USERINFO = re.compile(r"(\w+://)[^/@\s]+@")


def _is_sensitive(key: str, extra: frozenset[str]) -> bool:
    k = str(key)
    if k.lower() in _NOT_SECRET_KEYS:
        return False
    return bool(_SENSITIVE_KEY.search(k)) or k.lower() in extra


def _collect_secrets(obj: Any, out: set[str], extra: frozenset[str], force: bool = False) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            _collect_secrets(v, out, extra, force or _is_sensitive(k, extra))
    elif isinstance(obj, list):
        for v in obj:
            _collect_secrets(v, out, extra, force)
    elif force and isinstance(obj, str) and obj:
        out.add(obj)


def _scrub(obj: Any, values: set[str], extra: frozenset[str], force: bool = False) -> Any:
    if isinstance(obj, dict):
        return {
            k: _scrub(v, values, extra, force or _is_sensitive(k, extra)) for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_scrub(v, values, extra, force) for v in obj]
    if force and isinstance(obj, (str, int, float)) and not isinstance(obj, bool):
        return mask_secret(str(obj)) if obj != "" else obj
    if isinstance(obj, str):
        return scrub_text(obj, values)
    return obj


def scrub_text(text: str, values: set[str] | frozenset[str] = frozenset()) -> str:
    """Mask known secret values, Adafruit IO keys and URL credentials inside free text."""
    text = mask_values(text, values)
    text = _AIO_KEY.sub(lambda m: mask_secret(m.group(0)), text)
    return _URL_USERINFO.sub(r"\1****@", text)


def redact(obj: Any, *, extra_keys: frozenset[str] = frozenset(), values: set[str] | None = None):
    """Deep-copy ``obj`` with every secret masked (``****last4``, see redact.py).

    Values under secret-named keys (``secrets``, ``*_key``, ``password``, ``pat``, …)
    are masked whole; the same values are then masked wherever else they appear
    (e.g. a PAT spliced into a clone URL), as are ``aio_…`` keys and URL userinfo.
    """
    found: set[str] = set(values or ())
    _collect_secrets(obj, found, extra_keys)
    return _scrub(obj, found, extra_keys)


def _pretty(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=False, default=str)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _ctx(token: str, active: str, ops_page: str, **extra: Any) -> dict[str, Any]:
    """Template context for an ops page (base.html needs ``token`` + ``active``)."""
    return {"token": token, "active": active, "ops_page": ops_page, "ops_nav": OPS_NAV, **extra}


def _csv(value: str) -> list[str]:
    return [p.strip() for p in (value or "").split(",") if p.strip()]


async def _device_choices(db_path: str) -> list[dict[str, Any]]:
    async with get_db(db_path) as db:
        async with db.execute(
            "SELECT id, host_id, kind, model, pool, status, unavailable_reason "
            "FROM devices ORDER BY id"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def _registry(request: Request) -> Any:
    """The scheduler's live host registry, or a DB-only one for a headless controller."""
    registry = getattr(request.app.state, "host_registry", None)
    if registry is not None:
        return registry
    from hil_controller.hosts.registry import RealHostRegistry

    registry = RealHostRegistry(topology_file="", db_path=request.app.state.db_path)
    await registry.refresh_from_db()
    return registry


def _validation_messages(exc: ValidationError) -> list[str]:
    """Pydantic errors as ``loc: msg`` lines — never the offending input value."""
    out = []
    for err in exc.errors(include_url=False, include_input=False, include_context=False):
        loc = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
        out.append(f"{loc}: {err.get('msg', 'invalid')}")
    return out


# ---------------------------------------------------------------------------
# 1. Resolve dry-run
# ---------------------------------------------------------------------------


async def resolve_target(request: Request, principal: Principal, target: dict) -> dict[str, Any]:
    """Dry-run a job ``target``: the scheduler's pick plus the topology-resolve view.

    * **Scheduler** — ``HostRegistry.find_device_for_job`` (what ``get_adapter`` runs
      when the job is dequeued, including ``target.requires`` I2C-strand matching)
      followed by the same live-DB availability gate.
    * **Topology resolve** — ``api.topology.resolve_topology`` called in-process
      (identical to ``POST /v1/topology/resolve``) for aux/connection requirements,
      with each candidate's strand route checked against the strand requirements.
    """
    from hil_controller.api.jobs import Target
    from hil_controller.api.topology import AuxSelector, ResolveTarget, resolve_topology
    from hil_controller.hosts.registry import HostRegistry

    result: dict[str, Any] = {"errors": [], "target": target}
    try:
        parsed = Target.model_validate(target)
    except ValidationError as exc:
        result["errors"] = _validation_messages(exc)
        return result
    target = parsed.model_dump()
    result["target"] = target
    strand_reqs = [
        r for r in target["requires"] if r.get("kind") in HostRegistry.STRAND_REQUIRE_KINDS
    ]
    aux_reqs = [r for r in target["requires"] if r not in strand_reqs]
    registry = await _registry(request)
    strand_caps = HostRegistry._required_strand_caps(target)
    result["strand_caps"] = sorted(strand_caps)
    result["aux_reqs"] = aux_reqs

    # --- scheduler (authoritative) ---
    job_like = {"target": target}
    pick = registry.find_device_for_job(job_like)
    sched: dict[str, Any] = {"matched": pick is not None}
    if pick is None:
        sched["reason"] = registry._no_match_reason(job_like)
        if strand_caps:
            sched["reason"] += (
                f" — strand requirement {sorted(strand_caps)}: the device must be routed to "
                "ONE strand whose components (capability tags or model short-names) cover all."
            )
    else:
        host, device = pick
        sched.update(host_id=host["id"], device_id=device["id"])
        why = []
        sel = target["device"]
        if sel.get("id"):
            why.append(f"device.id={sel['id']} is authoritative (pool/kind/model gates skipped)")
        else:
            why.append(f"first available device in pool={target['pool']!r}")
            for field in ("kind", "model"):
                if sel.get(field):
                    why.append(f"{field}={sel[field]!r} matched")
            if sel.get("capabilities"):
                why.append(f"has capabilities {sel['capabilities']}")
        if strand_caps:
            route = registry.strand_for_device(device["id"], strand_caps)
            if route:
                sched["strand"] = {"strand_id": route["strand_id"], "channel": route["channel"]}
                why.append(
                    f"strand {route['strand_id']} (mux ch {route['channel']}) provides "
                    f"{sorted(strand_caps)}; select_i2c_strand would be auto-prepended"
                )
        async with get_db(request.app.state.db_path) as db:
            async with db.execute(
                "SELECT status, unavailable_reason FROM devices WHERE id = ?", (device["id"],)
            ) as cur:
                drow = await cur.fetchone()
        if drow is not None and drow["status"] == "unavailable":
            sched["gate_rejected"] = (
                f"device {device['id']} unavailable: "
                f"{drow['unavailable_reason'] or 'device unavailable'}"
            )
        sched["why"] = why
    result["scheduler"] = sched

    # --- topology resolve (same function as POST /v1/topology/resolve) ---
    body = ResolveTarget(
        device=target["device"],
        requires=[AuxSelector(**r) for r in aux_reqs],
        pool=target["pool"],
    )
    topo: dict[str, Any] = {"candidates": [], "rejected": []}
    try:
        resp = await resolve_topology(request, body, principal)
        topo["candidates"] = [c.model_dump() for c in resp.candidates]
        topo["rejected"] = list(resp.rejected)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        topo["rejected"] = list(detail.get("rejected") or [])
        topo["message"] = detail.get("message")
    if strand_caps:
        for cand in topo["candidates"]:
            route = registry.strand_for_device(cand["device_id"], strand_caps)
            cand["strand"] = (
                {"strand_id": route["strand_id"], "channel": route["channel"]} if route else None
            )
            if route is None:
                topo["rejected"].append(
                    {
                        "reason": "no_strand",
                        "detail": f"No strand routed to {cand['device_id']} provides "
                        f"{sorted(strand_caps)}",
                    }
                )
        topo["candidates"] = [c for c in topo["candidates"] if c.get("strand")]
    result["topology"] = topo
    result["target_pretty"] = _pretty(target)
    return result


def _target_from_form(
    *,
    pool: str,
    device_id: str,
    kind: str,
    model: str,
    capabilities: str,
    strand_caps: str,
    aux_kind: str,
    aux_caps: str,
) -> dict[str, Any]:
    device: dict[str, Any] = {}
    if device_id:
        device["id"] = device_id
    if kind:
        device["kind"] = kind
    if model:
        device["model"] = model
    if _csv(capabilities):
        device["capabilities"] = _csv(capabilities)
    requires: list[dict[str, Any]] = []
    if _csv(strand_caps):
        requires.append({"kind": "i2c_strand", "capabilities": _csv(strand_caps)})
    if aux_kind or _csv(aux_caps):
        requires.append({"kind": aux_kind or None, "capabilities": _csv(aux_caps)})
    return {"device": device, "requires": requires, "pool": pool or "public"}


@router.get("/jobs/resolve", response_class=HTMLResponse, include_in_schema=False)
async def resolve_page(
    request: Request,
    hil_token: str = Cookie(default=""),
    pool: str = "public",
    device_id: str = "",
    kind: str = "",
    model: str = "",
    capabilities: str = "",
    strand_caps: str = "",
    aux_kind: str = "",
    aux_caps: str = "",
    target_json: str = "",
    run: str = "",
) -> Response:
    principal = await _check_web_token(request, hil_token)
    if not principal:
        return _login_redirect()
    form = {
        "pool": pool,
        "device_id": device_id,
        "kind": kind,
        "model": model,
        "capabilities": capabilities,
        "strand_caps": strand_caps,
        "aux_kind": aux_kind,
        "aux_caps": aux_caps,
        "target_json": target_json,
    }
    outcome = None
    if run:
        if target_json.strip():
            try:
                target = json.loads(target_json)
            except json.JSONDecodeError as exc:
                target = None
                outcome = {
                    "errors": [
                        f"Target JSON is not valid JSON: {exc.msg} "
                        f"(line {exc.lineno}, column {exc.colno})"
                    ],
                }
            if target is not None and isinstance(target, dict) and "target" in target:
                target = target["target"]  # a whole job request was pasted
            if target is not None and not isinstance(target, dict):
                outcome = {"errors": ['Target JSON must be an object like {"device": {...}}']}
                target = None
        else:
            target = _target_from_form(
                pool=pool,
                device_id=device_id,
                kind=kind,
                model=model,
                capabilities=capabilities,
                strand_caps=strand_caps,
                aux_kind=aux_kind,
                aux_caps=aux_caps,
            )
        if target is not None:
            outcome = await resolve_target(request, principal, target)
    return _tr(
        request,
        "ops_resolve.html",
        _ctx(
            hil_token,
            "jobs",
            "resolve",
            title="Resolve target",
            form=form,
            outcome=outcome,
            devices=await _device_choices(request.app.state.db_path),
        ),
    )


# ---------------------------------------------------------------------------
# 2. Job profiles (job_guide) + recipes
# ---------------------------------------------------------------------------

#: AgentJob recipes (docs/mcp.md "Workflow and examples"); no real credentials.
RECIPES: dict[str, dict[str, Any]] = {
    "arduino-checkin": {
        "title": "WipperSnapper Arduino — protoMQ check-in (already flashed)",
        "summary": "Power-cycle, write secrets.json over MSC, reboot, wait for a v2 broker "
        "check-in on a per-job protoMQ. Add firmware {path|url+sha256} to flash first.",
        "job": {"runtime": "wippersnapper-arduino", "device_id": "DISCOVERED_MCU_ID"},
    },
    "python-pytest": {
        "title": "WipperSnapper Python — selected hardware test on an SBC",
        "summary": "Clone a source ref on the python-snapper host and run an explicit pytest "
        "entry; protoMQ launches on the controller. See hil-display-pytest.",
        "job": {
            "runtime": "wippersnapper-python",
            "device_id": "DISCOVERED_SBC_ID",
            "pool": "wippersnapper-python",
            "source": {
                "repo": "https://github.com/adafruit/Adafruit_Wippersnapper_Python.git",
                "ref": "SELECT_TESTED_REF",
            },
            "params": {
                "entry": "/path/to/bench/venv/bin/python",
                "args": ["-m", "pytest", "PATH_TO_SELECTED_HARDWARE_TEST", "-v"],
            },
        },
    },
    "circuitpython-code": {
        "title": "CircuitPython — deploy code.py and assert a serial marker",
        "summary": "Write UTF-8 files + settings.toml to CIRCUITPY, reset, and assert the "
        "marker appears after the final reset. Proves deployment, not networking.",
        "job": {
            "runtime": "circuitpython",
            "device_id": "DISCOVERED_CIRCUITPY_ID",
            "files": {
                "code.py": "import os\nassert os.getenv('CIRCUITPY_WIFI_SSID')\n"
                "print('HIL_CPY_EXAMPLE_OK')\n"
            },
            "expect_serial": "HIL_CPY_EXAMPLE_OK",
        },
    },
    "staging-io-checkin": {
        "title": "WipperSnapper v2 on staging IO — real-stack check-in (io_ws_expect)",
        "summary": "broker=staging (io.adafruit.us). Writes staging routing into secrets.json, "
        "then waits for a ws-b2w checkin frame around a guarded power_cycle. Needs your own "
        "staging IO_USERNAME / IO_KEY (replace REPLACE_ME). See examples/io-staging-ws-v2.",
        "job": {
            "runtime": "wippersnapper-arduino",
            "device_id": "DISCOVERED_MCU_ID",
            "broker": "staging",
            "secrets": {"IO_USERNAME": "REPLACE_ME", "IO_KEY": "REPLACE_ME"},
            "total_s": 1200,
            "params": {
                "window_minutes": 0,
                "stages": [
                    {"type": "power_cycle"},
                    {"type": "write_secrets_msc"},
                    {
                        "type": "io_ws_expect",
                        "label": "checkin",
                        "action": "checkin",
                        "timeout_s": 150,
                        "step": {"type": "power_cycle"},
                    },
                ],
            },
        },
    },
}


@router.get("/jobs/profiles", response_class=HTMLResponse, include_in_schema=False)
async def profiles_page(request: Request, hil_token: str = Cookie(default="")) -> Response:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    from hil_controller.job_profiles import IO_STACK_BROKERS, IO_STACK_STAGES, AgentJob, job_guide

    guide = job_guide()
    recipes = [{"key": k, **v, "json": _pretty(redact(v["job"]))} for k, v in RECIPES.items()]
    return _tr(
        request,
        "ops_profiles.html",
        _ctx(
            hil_token,
            "jobs",
            "profiles",
            title="Job profiles",
            guide=guide,
            recipes=recipes,
            io_stages=sorted(IO_STACK_STAGES),
            io_brokers=sorted(IO_STACK_BROKERS),
            schema_json=_pretty(AgentJob.model_json_schema()),
        ),
    )


# ---------------------------------------------------------------------------
# 3. Raw JSON job (validate / submit)
# ---------------------------------------------------------------------------

_BROKER_CHOICES = ("default", "staging", "dev-lan")
_PLACEHOLDER_SECRETS = frozenset({"REPLACE_ME", "hil", "changeme", "placeholder", ""})
_STAGING_HOST = "io.adafruit.us"


def _detect_format(data: dict[str, Any], fmt: str) -> str:
    if fmt in ("job-request", "agent-job"):
        return fmt
    return "agent-job" if "runtime" in data else "job-request"


def _apply_broker_to_request(
    req: dict[str, Any], broker: str, host: str | None, port: int | None
) -> list[str]:
    """Route a raw JobRequest at staging / dev-lan the way ``build_job`` does for AgentJob.

    Stamps the profile host onto io_* stages (``io_host``/``io_port``) and the DUT
    secrets stages (``io_url``/``io_port``), and selects the matching secrets profile.
    Returns error strings (empty on success).
    """
    from hil_controller.adapters.bench_stages import walk_stages
    from hil_controller.job_profiles import _IO_STAGE_RESERVED, IO_STACK_STAGES

    errors: list[str] = []
    if broker == "staging":
        host, port = _STAGING_HOST, port or 8883
    if not host or not port or any(c in host for c in "/@?# \r\n"):
        return ["dev-lan needs a broker host (hostname or IP, no scheme/credentials) and port"]
    if host.lower().endswith("adafruit.com"):
        return ["Production IO is not a raw-submit broker target; use staging or dev-lan"]
    port = port or 8883
    secrets = req.get("secrets") or {}
    if any(
        str(secrets.get(k, "")).strip() in _PLACEHOLDER_SECRETS for k in ("IO_USERNAME", "IO_KEY")
    ):
        errors.append(f"broker={broker} needs your own IO_USERNAME and IO_KEY in secrets")
    req["secrets_profile"] = {"staging": "adafruit-staging", "dev-lan": "dev-lan"}[broker]
    req.setdefault("metadata", {})["broker_mode"] = broker
    stages = (req.get("params") or {}).get("stages") or []
    for stage in walk_stages(stages if isinstance(stages, list) else []):
        if not isinstance(stage, dict):
            continue
        kind = stage.get("type") or ""
        if kind in {"launch_protomq", "verify_checkin"} or kind.startswith("inject_"):
            errors.append(f"{kind} inspects local protoMQ only; it cannot run against {broker}")
        if kind in IO_STACK_STAGES:
            if any(k in stage for k in _IO_STAGE_RESERVED):
                errors.append(f"{kind}: IO host and credentials come from the broker/secrets")
            if broker != "dev-lan" and ("io_api_base" in stage or "tls" in stage):
                errors.append(f"{kind}: io_api_base / tls overrides are only for dev-lan")
            stage.update(io_host=host, io_port=port)
        if kind in {"write_secrets_msc", "write_circuitpython"}:
            stage.update(io_url=host, io_port=port)
    return errors


def prepare_raw_job(
    text: str,
    *,
    fmt: str = "auto",
    broker: str = "default",
    broker_host: str = "",
    broker_port: str = "",
    device_id: str = "",
) -> dict[str, Any]:
    """Validate raw JSON into the exact ``JobRequest`` dict ``POST /v1/jobs`` would store.

    Returns ``{"errors", "warnings", "format", "request", "secret_values"}``; ``request``
    is UNREDACTED and must only be shown through :func:`redact`.
    """
    from hil_controller.adapters.bench_stages import StageError, validate_stages, walk_stages
    from hil_controller.api.jobs import JobRequest
    from hil_controller.job_profiles import IO_STACK_STAGES, AgentJob, build_job

    out: dict[str, Any] = {"errors": [], "warnings": [], "format": None, "request": None}
    secret_values: set[str] = set()
    out["secret_values"] = secret_values
    if not text.strip():
        out["errors"].append("Paste a JSON job (a /v1/jobs JobRequest or an MCP AgentJob).")
        return out
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        out["errors"].append(f"Invalid JSON: {exc.msg} at line {exc.lineno}, column {exc.colno}")
        return out
    if not isinstance(data, dict):
        out["errors"].append("The job must be a JSON object, not " + type(data).__name__)
        return out
    _collect_secrets(data, secret_values, frozenset())
    if broker not in _BROKER_CHOICES:
        out["errors"].append(f"Unknown broker {broker!r}; choose one of {list(_BROKER_CHOICES)}")
        return out
    port: int | None = None
    if broker_port.strip():
        try:
            port = int(broker_port)
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            out["errors"].append("Broker port must be an integer 1–65535")
            return out
    kind = _detect_format(data, fmt)
    out["format"] = kind

    try:
        if kind == "agent-job":
            if device_id:
                data["device_id"] = device_id
            if broker != "default":
                data["broker"] = broker
                if broker == "dev-lan":
                    if broker_host:
                        data["broker_host"] = broker_host
                    if port:
                        data["broker_port"] = port
            try:
                job = AgentJob.model_validate(data)
            except ValidationError as exc:
                out["errors"].extend(_validation_messages(exc))
                return out
            placeholders = [
                k for k, v in job.secrets.items() if v.get_secret_value() == "REPLACE_ME"
            ]
            if placeholders:
                out["errors"].append(
                    f"Replace the placeholder secret values: {sorted(placeholders)}"
                )
                return out
            request = build_job(job)
        else:
            if device_id:
                data.setdefault("target", {})
                if isinstance(data["target"], dict):
                    data["target"]["device"] = {"id": device_id}
            try:
                request = JobRequest.model_validate(data).model_dump()
            except ValidationError as exc:
                out["errors"].extend(_validation_messages(exc))
                return out
            if broker != "default":
                errs = _apply_broker_to_request(request, broker, broker_host.strip() or None, port)
                if errs:
                    out["errors"].extend(errs)
                    return out
            stages = request["params"].get("stages")
            if stages is not None:
                if not isinstance(stages, list):
                    out["errors"].append("params.stages must be a list of stage objects")
                    return out
                validate_stages(stages)
                for stage in walk_stages(stages):
                    if stage.get("type") in IO_STACK_STAGES and not stage.get("io_host"):
                        out["warnings"].append(
                            f"{stage['type']} has no io_host: pick broker staging or dev-lan "
                            "(or set io_host on the stage) or it fails at run time"
                        )
            request = JobRequest.model_validate(request).model_dump()
    except (StageError, ValueError) as exc:
        out["errors"].append(scrub_text(str(exc), secret_values))
        return out

    request.setdefault("metadata", {}).setdefault("submitted_via", "ui/jobs/raw")
    if request["script"] in ("firmware-bench", "raw-firmware-smoke"):
        out["warnings"].append(
            f"script={request['script']} acquires real hardware and may flash it "
            "(token needs the trusted-firmware capability)."
        )
    _collect_secrets(request, secret_values, frozenset())
    out["request"] = request
    return out


def _raw_ctx(prepared: dict[str, Any]) -> dict[str, Any]:
    req = prepared.get("request")
    preview = None
    if req is not None:
        preview = _pretty(redact(req, values=prepared["secret_values"]))
    return {
        "errors": [scrub_text(e, prepared["secret_values"]) for e in prepared["errors"]],
        "warnings": prepared["warnings"],
        "format": prepared["format"],
        "preview": preview,
    }


@router.get("/jobs/raw", response_class=HTMLResponse, include_in_schema=False)
async def raw_job_page(
    request: Request,
    hil_token: str = Cookie(default=""),
    recipe: str = "",
) -> Response:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    initial = ""
    if recipe in RECIPES:
        initial = _pretty(RECIPES[recipe]["job"])
    return _tr(
        request,
        "ops_raw.html",
        _ctx(
            hil_token,
            "jobs",
            "raw",
            title="Raw JSON job",
            initial=initial,
            recipe=recipe if recipe in RECIPES else "",
            recipe_broker=(RECIPES.get(recipe) or {}).get("job", {}).get("broker", "default"),
            recipes=RECIPES,
            brokers=_BROKER_CHOICES,
            devices=await _device_choices(request.app.state.db_path),
        ),
    )


@router.post("/jobs/raw/validate", response_class=HTMLResponse, include_in_schema=False)
async def raw_job_validate(
    request: Request,
    hil_token: str = Cookie(default=""),
    job_json: str = Form(default=""),
    fmt: str = Form(default="auto"),
    broker: str = Form(default="default"),
    broker_host: str = Form(default=""),
    broker_port: str = Form(default=""),
    device_id: str = Form(default=""),
    resolve: str = Form(default=""),
) -> Response:
    principal = await _check_web_token(request, hil_token)
    if not principal:
        return HTMLResponse("Not authenticated", status_code=status.HTTP_401_UNAUTHORIZED)
    prepared = prepare_raw_job(
        job_json,
        fmt=fmt,
        broker=broker,
        broker_host=broker_host,
        broker_port=broker_port,
        device_id=device_id,
    )
    ctx = _raw_ctx(prepared)
    ctx["mode"] = "validate"
    if prepared["request"] is not None and resolve:
        ctx["resolution"] = await resolve_target(request, principal, prepared["request"]["target"])
    return _tr(request, "ops_raw_result.html", ctx)


@router.post("/jobs/raw/submit", include_in_schema=False, response_model=None)
async def raw_job_submit(
    request: Request,
    hil_token: str = Cookie(default=""),
    job_json: str = Form(default=""),
    fmt: str = Form(default="auto"),
    broker: str = Form(default="default"),
    broker_host: str = Form(default=""),
    broker_port: str = Form(default=""),
    device_id: str = Form(default=""),
) -> Response:
    principal = await _check_web_token(request, hil_token)
    if not principal:
        return HTMLResponse("Not authenticated", status_code=status.HTTP_401_UNAUTHORIZED)
    prepared = prepare_raw_job(
        job_json,
        fmt=fmt,
        broker=broker,
        broker_host=broker_host,
        broker_port=broker_port,
        device_id=device_id,
    )
    if prepared["request"] is not None:
        from hil_controller.api.jobs import JobRequest
        from hil_controller.api.jobs import submit_job as api_submit_job

        try:
            # The exact handler behind POST /v1/jobs (pool/profile/trusted-firmware
            # checks, insert, audit, enqueue), called in-process with the web principal.
            resp = await api_submit_job(
                request, JobRequest.model_validate(prepared["request"]), principal
            )
        except HTTPException as exc:
            prepared["errors"].append(f"Rejected (HTTP {exc.status_code}): {exc.detail}")
        else:
            target = f"/ui/jobs/{resp.id}"
            if request.headers.get("HX-Request"):
                return Response(status_code=200, headers={"HX-Redirect": target})
            return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
    ctx = _raw_ctx(prepared)
    ctx["mode"] = "submit"
    return _tr(request, "ops_raw_result.html", ctx, status_code=422)


# ---------------------------------------------------------------------------
# 4. Skills browser
# ---------------------------------------------------------------------------


class _LocalSkillStore:
    """Fallback mirroring ``mcp_server.SkillStore`` when the ``mcp`` extra is absent."""

    def __init__(self, root: Path | None = None):
        here = Path(__file__).resolve().parents[1]
        checkout = here.parents[1] / ".agent" / "skills"
        bundled = here / "agent_skills"
        self.root = (root or (checkout if checkout.is_dir() else bundled)).resolve()

    def catalog(self) -> list[dict[str, str]]:
        import hashlib

        return [
            {"name": p.parent.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted(self.root.glob("*/SKILL.md"))
            if p.resolve().is_relative_to(self.root)
        ]

    def read(self, name: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            raise ValueError("Choose a skill name from list_skills")
        path = (self.root / name / "SKILL.md").resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError("Skill is unavailable; call list_skills again")
        return path.read_text(encoding="utf-8")


def skill_store() -> Any:
    """The MCP server's SkillStore (same root + ``HIL_SKILLS_DIR`` override as ``hil-mcp``)."""
    root = os.environ.get("HIL_SKILLS_DIR")
    root_path = Path(root) if root else None
    try:
        from hil_controller.mcp_server import SkillStore
    except ImportError:  # controller installed without the [mcp] extra
        return _LocalSkillStore(root_path)
    return SkillStore(root_path)


def _front_matter(text: str) -> tuple[dict[str, str], str]:
    """Split a ``---`` YAML front-matter block (name/description) from the body."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    meta: dict[str, str] = {}
    try:
        import yaml

        loaded = yaml.safe_load(text[3:end])
        if isinstance(loaded, dict):
            meta = {str(k): str(v) for k, v in loaded.items()}
    except Exception:  # noqa: BLE001 — malformed front matter is shown as text
        return {}, text
    return meta, text[end + 4 :].lstrip("\n")


_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")


def _inline(escaped: str) -> str:
    """Inline `code` / **bold** on ALREADY-ESCAPED text (only adds our own tags)."""
    escaped = _INLINE_CODE.sub(r"<code>\1</code>", escaped)
    return _BOLD.sub(r"<strong>\1</strong>", escaped)


def render_markdown_safe(text: str) -> str:
    """Minimal, escape-first markdown → HTML (no markdown dependency, no raw HTML).

    Every character of the source is ``html.escape``d before any tag is added.
    Headings become ``<hN>``, fenced code becomes ``<pre><code>``, and everything
    else is kept verbatim in ``<pre class="md-text">`` runs (tables, lists, prose)
    with inline code/bold highlighted.
    """
    out: list[str] = []
    buf: list[str] = []
    code: list[str] | None = None
    lang = ""

    def flush() -> None:
        while buf and not buf[-1].strip():
            buf.pop()
        while buf and not buf[0].strip():
            buf.pop(0)
        if buf:
            body = "\n".join(_inline(html.escape(line)) for line in buf)
            out.append(f'<pre class="md-text">{body}</pre>')
        buf.clear()

    for line in text.splitlines():
        fence = re.match(r"^\s*(```|~~~)\s*([\w+-]*)", line)
        if code is not None:
            if fence:
                cls = f' class="lang-{html.escape(lang)}"' if lang else ""
                out.append(
                    f'<pre class="md-code"><code{cls}>'
                    f"{html.escape(chr(10).join(code))}</code></pre>"
                )
                code = None
            else:
                code.append(line)
            continue
        if fence:
            flush()
            code, lang = [], fence.group(2)
            continue
        heading = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if heading:
            flush()
            tag = f"h{min(len(heading.group(1)) + 1, 6)}"  # the page owns <h1>
            out.append(f'<{tag} class="md-h">{_inline(html.escape(heading.group(2)))}</{tag}>')
            continue
        buf.append(line)
    if code is not None:  # unterminated fence: still escaped
        out.append(f'<pre class="md-code"><code>{html.escape(chr(10).join(code))}</code></pre>')
    flush()
    return "\n".join(out)


@router.get("/skills", response_class=HTMLResponse, include_in_schema=False)
async def skills_page(request: Request, hil_token: str = Cookie(default="")) -> Response:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    store = skill_store()
    skills = []
    for entry in store.catalog():
        try:
            meta, _ = _front_matter(store.read(entry["name"]))
        except ValueError:
            meta = {}
        skills.append({**entry, "description": meta.get("description", "")})
    return _tr(
        request,
        "ops_skills.html",
        _ctx(hil_token, "skills", "skills", title="Skills", skills=skills, root=str(store.root)),
    )


@router.get("/skills/{name}", response_class=HTMLResponse, include_in_schema=False)
async def skill_detail_page(
    request: Request, name: str, hil_token: str = Cookie(default="")
) -> Response:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    store = skill_store()
    try:
        text = store.read(name)
    except ValueError as exc:
        return _tr(
            request,
            "ops_skill.html",
            _ctx(hil_token, "skills", "skills", title="Skill", name=name, error=str(exc)),
            status_code=404,
        )
    import hashlib

    meta, body = _front_matter(text)
    return _tr(
        request,
        "ops_skill.html",
        _ctx(
            hil_token,
            "skills",
            "skills",
            title=f"Skill {name}",
            name=name,
            meta=meta,
            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            body_html=render_markdown_safe(body),
            error=None,
        ),
    )


# ---------------------------------------------------------------------------
# 5. Audit log
# ---------------------------------------------------------------------------

#: audit_log detail keys that hold partial credentials (auth.fail logs a token prefix).
_AUDIT_SENSITIVE = frozenset({"hint"})


@router.get("/audit", response_class=HTMLResponse, include_in_schema=False)
async def audit_page(
    request: Request,
    hil_token: str = Cookie(default=""),
    event: str = "",
    entity_id: str = "",
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=500),
) -> Response:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    where: list[str] = []
    params: list[Any] = []
    if event:
        if event.endswith("*"):
            where.append("event LIKE ? ESCAPE '\\'")
            params.append(_like_escape(event[:-1]) + "%")
        else:
            where.append("event = ?")
            params.append(event)
    if entity_id:
        where.append("entity_id LIKE ? ESCAPE '\\'")
        params.append("%" + _like_escape(entity_id) + "%")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    async with get_db(request.app.state.db_path) as db:
        async with db.execute(f"SELECT COUNT(*) FROM audit_log {clause}", params) as cur:
            total = (await cur.fetchone())[0]
        pages = max(1, math.ceil(total / per_page))
        page = min(page, pages)
        async with db.execute(
            f"SELECT * FROM audit_log {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, per_page, (page - 1) * per_page],
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        async with db.execute("SELECT DISTINCT event FROM audit_log ORDER BY event") as cur:
            events = [r[0] for r in await cur.fetchall()]
    for r in rows:
        try:
            detail = json.loads(r.get("detail_json") or "{}")
        except json.JSONDecodeError:
            detail = r.get("detail_json") or ""
        detail = redact(detail, extra_keys=_AUDIT_SENSITIVE)
        r["detail_pretty"] = _pretty(detail) if not isinstance(detail, str) else detail
        r["detail_short"] = _summary(detail)
    from urllib.parse import urlencode

    def page_url(n: int) -> str:
        q = {"event": event, "entity_id": entity_id, "page": n, "per_page": per_page}
        return "/ui/audit?" + urlencode({k: v for k, v in q.items() if v not in ("", None)})

    return _tr(
        request,
        "ops_audit.html",
        _ctx(
            hil_token,
            "audit",
            "audit",
            title="Audit log",
            rows=rows,
            events=events,
            filters={"event": event, "entity_id": entity_id, "per_page": per_page},
            page=page,
            pages=pages,
            total=total,
            prev_url=page_url(page - 1) if page > 1 else None,
            next_url=page_url(page + 1) if page < pages else None,
        ),
    )


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _summary(detail: Any, limit: int = 120) -> str:
    if isinstance(detail, dict):
        text = ", ".join(f"{k}={json.dumps(v, default=str)}" for k, v in detail.items())
    else:
        text = str(detail)
    return text if len(text) <= limit else text[: limit - 1] + "…"


__all__ = [
    "RECIPES",
    "prepare_raw_job",
    "redact",
    "render_markdown_safe",
    "resolve_target",
    "router",
    "skill_store",
]
