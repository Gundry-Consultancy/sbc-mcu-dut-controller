"""Physical Twins web UI: inventory list/detail, slots, observation preview/apply.

Every route mirrors the administrative JSON API in ``hil_controller.api.twins``:
the same ``inventory:read`` / ``inventory:write`` + all-pools authorization, the
same BEGIN IMMEDIATE + rollback preview, the same ``twins.ingest`` apply, and the
same close-connection validation. Collection never auto-applies: bridge
observations always land on the preview screen first. Bridge tokens are read
from the controller environment by name and are never rendered.
"""

from __future__ import annotations

import json
import os
import shlex
import sqlite3
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Cookie, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from hil_controller import twins
from hil_controller.adapters.esp_usbip_bridge import bridge_base_url
from hil_controller.auth.principal import Principal
from hil_controller.db.connection import get_db
from hil_controller.twin_collectors import CollectionError, collect_bridge
from hil_controller.twin_models import CloseConnection, Observation
from hil_controller.web.router import _check_web_token, _login_redirect, _tr

router = APIRouter(prefix="/ui", tags=["web"])

SLOT_KINDS = list(twins.SLOT_TABLES)
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_OBSERVATIONS = 500
LIST_LIMIT = 500
BRIDGE_TRANSPORT = "esp-usbip-bridge"
LINUX_TRANSPORTS = ("ssh", "local")


# ---------------------------------------------------------------------------
# Auth: identical policy to api/twins.py authorize(), rendered as a 403 page.
# ---------------------------------------------------------------------------


def _missing(principal: Principal, *, write: bool) -> list[str]:
    """What the principal lacks for physical inventory access (empty = allowed)."""
    missing = []
    if write:
        if not principal.has_capability("inventory:write"):
            missing.append("capability inventory:write")
    elif not (
        principal.has_capability("inventory:read") or principal.has_capability("inventory:write")
    ):
        missing.append("capability inventory:read (or inventory:write)")
    if not principal.allows_pool("*"):
        missing.append('access to all pools (allowed_pools: ["*"])')
    return missing


async def _guard(
    request: Request, hil_token: str, *, write: bool = False
) -> tuple[Principal | None, Response | None]:
    """Return (principal, None) when allowed, else (None, redirect-or-403 response)."""
    principal = await _check_web_token(request, hil_token)
    if principal is None:
        return None, _login_redirect()
    missing = _missing(principal, write=write)
    if missing:
        return None, _tr(
            request,
            "twins_message.html",
            {
                "token": hil_token,
                "active": "twins",
                "title": "Physical twins — access denied",
                "heading": "Physical inventory access denied",
                "status_code": 403,
                "missing": missing,
                "write": write,
                "principal_caps": principal.capabilities,
                "principal_pools": principal.allowed_pools,
            },
            status_code=403,
        )
    return principal, None


def _message(
    request: Request, hil_token: str, status_code: int, heading: str, detail: str
) -> HTMLResponse:
    return _tr(
        request,
        "twins_message.html",
        {
            "token": hil_token,
            "active": "twins",
            "title": "Physical twins",
            "heading": heading,
            "detail": detail,
            "status_code": status_code,
        },
        status_code=status_code,
    )


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

_CURRENT_LABEL = (
    "COALESCE((SELECT json_extract(o.payload_json,'$.label') FROM twin_observations o "
    "WHERE o.twin_id=t.id AND json_extract(o.payload_json,'$.label') IS NOT NULL "
    "ORDER BY o.observed_at DESC,o.observation_id DESC LIMIT 1),t.label)"
)


def _like(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def _list_twins(
    db_path: str, kind: str | None, pool: str | None, q: str | None
) -> dict[str, Any]:
    pattern = _like(q) if q else None
    async with get_db(db_path) as db:
        items = await twins.rows(
            db,
            f"SELECT t.*, {_CURRENT_LABEL} AS current_label, "
            "(SELECT COUNT(*) FROM twin_identities i WHERE i.twin_id=t.id) AS identity_count, "
            "(SELECT COUNT(*) FROM twin_ports p WHERE p.twin_id=t.id) AS port_count, "
            "(SELECT COUNT(*) FROM twin_connections c WHERE c.ended_at IS NULL "
            " AND (c.from_twin_id=t.id OR c.to_twin_id=t.id)) AS open_connections, "
            "(SELECT o.observed_at FROM twin_observations o WHERE o.twin_id=t.id "
            " ORDER BY o.observed_at DESC,o.observation_id DESC LIMIT 1) AS last_observed_at, "
            "(SELECT o.source FROM twin_observations o WHERE o.twin_id=t.id "
            " ORDER BY o.observed_at DESC,o.observation_id DESC LIMIT 1) AS last_source "
            "FROM physical_twins t WHERE (? IS NULL OR t.kind=?) AND (? IS NULL OR t.pool=?) "
            "AND (? IS NULL OR t.id LIKE ? ESCAPE '\\' OR t.label LIKE ? ESCAPE '\\' "
            f" OR {_CURRENT_LABEL} LIKE ? ESCAPE '\\' "
            " OR EXISTS(SELECT 1 FROM twin_identities i WHERE i.twin_id=t.id "
            "   AND i.value LIKE ? ESCAPE '\\') "
            " OR EXISTS(SELECT 1 FROM twin_slot_assignments s WHERE s.twin_id=t.id "
            "   AND s.ended_at IS NULL AND s.slot_id LIKE ? ESCAPE '\\')) "
            "ORDER BY t.created_at DESC, t.id LIMIT ?",
            (kind, kind, pool, pool, pattern, *([pattern] * 5), LIST_LIMIT + 1),
        )
        slots = await twins.rows(
            db,
            "SELECT twin_id, slot_kind, slot_id, assigned_at FROM twin_slot_assignments "
            "WHERE ended_at IS NULL ORDER BY slot_kind, slot_id",
        )
        kinds = [r["kind"] for r in await twins.rows(
            db, "SELECT DISTINCT kind FROM physical_twins ORDER BY kind"
        )]
        pools = [r["pool"] for r in await twins.rows(
            db, "SELECT DISTINCT pool FROM physical_twins ORDER BY pool"
        )]
        total = (await twins.rows(db, "SELECT COUNT(*) AS n FROM physical_twins"))[0]["n"]
    by_twin: dict[str, list[dict[str, Any]]] = {}
    for slot in slots:
        by_twin.setdefault(slot["twin_id"], []).append(slot)
    truncated = len(items) > LIST_LIMIT
    for item in items[:LIST_LIMIT]:
        item["slots"] = by_twin.get(item["id"], [])
    return {
        "twins": items[:LIST_LIMIT],
        "truncated": truncated,
        "kinds": kinds,
        "pools": pools,
        "total": total,
    }


async def _collector_hosts(db_path: str) -> dict[str, list[dict[str, Any]]]:
    async with get_db(db_path) as db:
        hosts = await twins.rows(
            db, "SELECT id, addr, transport, ssh_user, api_url, token_env FROM hosts ORDER BY id"
        )
    bridges, linux = [], []
    for host in hosts:
        if host["transport"] == BRIDGE_TRANSPORT:
            env = host.get("token_env")
            bridges.append(
                {
                    "id": host["id"],
                    "addr": host["addr"],
                    "token_env": env,
                    # Only whether the variable is set; never its value.
                    "token_set": bool(env and os.environ.get(env)),
                }
            )
        elif host["transport"] in LINUX_TRANSPORTS:
            linux.append({**host, "commands": _linux_commands(host)})
    return {"bridges": bridges, "linux": linux}


def _linux_commands(host: dict[str, Any]) -> dict[str, str]:
    """The documented collector command (docs/twin-collectors.md) for one host."""
    scan_id = f"inventory-{datetime.now(UTC):%Y-%m-%d}-{host['id']}"
    collector = (
        "python -m hil_controller.twin_collectors "
        f"--host-id {shlex.quote(host['id'])} --scan-id {shlex.quote(scan_id)} "
        "--output observations.json linux"
    )
    stdout_collector = (
        "python -m hil_controller.twin_collectors "
        f"--host-id {shlex.quote(host['id'])} --scan-id {shlex.quote(scan_id)} linux"
    )
    commands = {"on_host": collector}
    if host["transport"] == "ssh" and host.get("addr"):
        target = f"{host.get('ssh_user') or 'pi'}@{host['addr']}"
        commands["via_ssh"] = (
            f"ssh {shlex.quote(target)} {shlex.quote(stdout_collector)} > observations.json"
        )
    return commands


async def _slots(db_path: str, kind: str) -> dict[str, Any]:
    table = twins.SLOT_TABLES[kind]  # fixed allowlist, never caller SQL
    async with get_db(db_path) as db:
        slots = await twins.rows(
            db,
            f"SELECT s.id,a.twin_id,a.assigned_at,a.observation_id FROM {table} s "
            "LEFT JOIN twin_slot_assignments a ON a.slot_id=s.id "
            "AND a.slot_kind=? AND a.ended_at IS NULL ORDER BY s.id",
            (kind,),
        )
        history = await twins.rows(
            db,
            "SELECT * FROM twin_slot_assignments WHERE slot_kind=? ORDER BY assigned_at,id",
            (kind,),
        )
        labels = {
            r["id"]: r["current_label"]
            for r in await twins.rows(
                db, f"SELECT t.id, {_CURRENT_LABEL} AS current_label FROM physical_twins t"
            )
        }
    existing = {s["id"] for s in slots}
    for row in [*slots, *history]:
        row["twin_label"] = labels.get(row.get("twin_id") or "")
    for row in history:
        row["slot_exists"] = row["slot_id"] in existing
    return {
        "slots": slots,
        "unassigned": [s for s in slots if s["twin_id"] is None],
        "history": list(reversed(history)),
    }


def _slot_link(kind: str, slot_id: str) -> str | None:
    if kind == "device":
        return f"/ui/devices/{slot_id}/form"
    if kind == "host":
        return "/ui/hosts"
    if kind == "camera":
        return "/ui/cameras"
    return None


# ---------------------------------------------------------------------------
# Observation parsing / preview / apply
# ---------------------------------------------------------------------------


class InputProblem(Exception):
    def __init__(self, status: int, message: str, errors: list[str] | None = None):
        self.status = status
        self.message = message
        self.errors = errors or []
        super().__init__(message)


def _documents(text: str) -> list[Any]:
    if not text.strip():
        raise InputProblem(422, "Paste or upload an Observation JSON document first.")
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InputProblem(
            422, f"Not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})"
        ) from None
    if isinstance(document, dict) and "observations" in document:
        extra = set(document) - {"observations", "connections"}
        if extra:
            raise InputProblem(422, "Batch objects may only contain observations/connections")
        if document.get("connections"):
            raise InputProblem(
                422,
                "Batch connections are not supported by the web import. Import observations "
                "here, then use hil-twins-import (or POST /v1/twins/connections) for wiring.",
            )
        document = document["observations"]
    if isinstance(document, dict):
        document = [document]
    if not isinstance(document, list) or not document:
        raise InputProblem(
            422, "Expected one Observation object, a non-empty list, or {observations: [...]}"
        )
    if len(document) > MAX_OBSERVATIONS:
        raise InputProblem(422, f"At most {MAX_OBSERVATIONS} observations per web import")
    return document


def _validate(documents: list[Any]) -> list[Observation]:
    observations, errors = [], []
    for index, value in enumerate(documents):
        try:
            observations.append(Observation.model_validate(value))
        except ValidationError as exc:
            # Location + message only: pydantic's default rendering echoes raw input.
            for err in exc.errors(include_input=False, include_url=False):
                loc = ".".join(str(part) for part in err["loc"]) or "(observation)"
                errors.append(f"observation[{index}] {loc}: {err['msg']}")
    if errors:
        raise InputProblem(422, "Observation schema validation failed (HTTP 422).", errors)
    return observations


def _normalized(observations: list[Observation]) -> str:
    return json.dumps(
        [o.model_dump(mode="json", exclude_none=False) for o in observations], indent=2
    )


def _action(observation: Observation, result: dict[str, Any]) -> str:
    if result["replayed"]:
        return "replay"
    return "update" if observation.twin_id else "create"


async def _preview(
    db_path: str, observations: list[Observation], actor: str
) -> list[dict[str, Any]]:
    """Mirror api/twins.py preview for a batch: one BEGIN IMMEDIATE, always rolled back.

    Each observation runs inside a savepoint so a conflicting entry does not leak
    partial writes into the evaluation of later entries; later entries do see the
    effects of earlier valid ones, exactly as the batch would be applied.
    """
    results: list[dict[str, Any]] = []
    async with get_db(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            for observation in observations:
                matches = await twins.candidates(db, observation)
                await db.execute("SAVEPOINT twin_ui_preview")
                entry: dict[str, Any] = {"observation": observation, "candidates": matches}
                try:
                    result = await twins.ingest(db, observation, actor)
                    # Generated twin IDs are provisional and deliberately not shown.
                    entry.update(
                        valid=True, action=_action(observation, result), replayed=result["replayed"]
                    )
                    await db.execute("RELEASE twin_ui_preview")
                except (twins.TwinError, sqlite3.IntegrityError) as exc:
                    conflict = (
                        {"status": exc.status, "detail": exc.detail}
                        if isinstance(exc, twins.TwinError)
                        else {"status": 409, "detail": "Database integrity conflict"}
                    )
                    entry.update(valid=False, conflict=conflict)
                    await db.execute("ROLLBACK TO twin_ui_preview")
                    await db.execute("RELEASE twin_ui_preview")
                results.append(entry)
        finally:
            await db.rollback()
    return results


async def _apply(
    db_path: str, observations: list[Observation], actor: str
) -> list[dict[str, Any]]:
    """Mirror api/twins.py observe: BEGIN IMMEDIATE, ingest, commit; roll back on TwinError.

    A pasted batch is all-or-nothing (like hil-twins-import), so a failure names the
    offending observation and nothing is written.
    """
    async with get_db(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        results = []
        index = 0
        try:
            for index, observation in enumerate(observations):
                results.append(await twins.ingest(db, observation, actor))
            await db.commit()
        except twins.TwinError as exc:
            await db.rollback()
            exc.detail = f"observation[{index}] {observations[index].observation_id}: {exc.detail}"
            raise
        except sqlite3.IntegrityError:
            await db.rollback()
            raise twins.TwinError(
                409,
                f"observation[{index}] {observations[index].observation_id}: "
                "database integrity conflict; re-preview",
            ) from None
    return results


async def _read_input(observations_json: str, upload: UploadFile | None) -> str:
    if upload is not None and upload.filename:
        data = await upload.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise InputProblem(413, "Uploaded file exceeds the 2 MiB web import limit")
        if data:
            try:
                return data.decode("utf-8-sig")
            except UnicodeDecodeError:
                raise InputProblem(422, "Uploaded file is not UTF-8 JSON") from None
    if len(observations_json.encode()) > MAX_UPLOAD_BYTES:
        raise InputProblem(413, "Pasted JSON exceeds the 2 MiB web import limit")
    return observations_json


def _preview_page(
    request: Request,
    hil_token: str,
    *,
    text: str,
    results: list[dict[str, Any]] | None = None,
    normalized: str | None = None,
    problem: InputProblem | None = None,
    origin: str | None = None,
    applied: list[dict[str, Any]] | None = None,
    apply_error: twins.TwinError | None = None,
) -> HTMLResponse:
    entries = []
    for entry in results or []:
        obs: Observation = entry["observation"]
        entries.append(
            {
                **entry,
                "pretty": json.dumps(obs.model_dump(mode="json"), indent=2),
            }
        )
    status_code = 200
    if problem is not None:
        status_code = problem.status
    elif apply_error is not None:
        status_code = apply_error.status
    all_valid = bool(entries) and all(e["valid"] for e in entries)
    return _tr(
        request,
        "twins_preview.html",
        {
            "token": hil_token,
            "active": "twins",
            "title": "Physical twins — observation preview",
            "text": text,
            "entries": entries,
            "normalized": normalized,
            "problem": problem,
            "origin": origin,
            "all_valid": all_valid,
            "applied": applied,
            "apply_error": apply_error,
            "status_code": status_code,
        },
        status_code=status_code,
    )


async def _preview_text(
    request: Request, hil_token: str, principal: Principal, text: str, origin: str | None = None
) -> HTMLResponse:
    try:
        observations = _validate(_documents(text))
    except InputProblem as problem:
        return _preview_page(request, hil_token, text=text, problem=problem, origin=origin)
    results = await _preview(request.app.state.db_path, observations, principal.subject)
    normalized = _normalized(observations)
    return _preview_page(
        request,
        hil_token,
        text=normalized,
        results=results,
        normalized=normalized,
        origin=origin,
    )


# ---------------------------------------------------------------------------
# Bridge collection: server-side, read-only, never auto-applied.
# ---------------------------------------------------------------------------


def _bridge_http_client() -> httpx.Client:
    """Client used for bridge collection (tests monkeypatch this factory)."""
    return httpx.Client(trust_env=False)


def _collect_from_bridge(host: dict[str, Any]) -> list[Observation]:
    base_url = bridge_base_url(host.get("addr") or "", host.get("api_url"))
    env = host.get("token_env")
    token_env = env if env and os.environ.get(env) else None
    observed_at = datetime.now(UTC)
    scan_id = f"ui-{observed_at:%Y%m%dT%H%M%S%fZ}"
    with _bridge_http_client() as client:
        return collect_bridge(
            base_url,
            host_id=host["id"],
            scan_id=scan_id,
            observed_at=observed_at,
            token_env=token_env,
            client=client,
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/twins", response_class=HTMLResponse, include_in_schema=False)
async def twins_page(
    request: Request,
    hil_token: str = Cookie(default=""),
    kind: str = "",
    pool: str = "",
    q: str = "",
) -> Response:
    principal, denied = await _guard(request, hil_token)
    if denied is not None:
        return denied
    db_path = request.app.state.db_path
    listing = await _list_twins(db_path, kind or None, pool or None, q.strip() or None)
    return _tr(
        request,
        "twins.html",
        {
            "token": hil_token,
            "active": "twins",
            "title": "Physical twins",
            **listing,
            **(await _collector_hosts(db_path)),
            "kind": kind,
            "pool": pool,
            "q": q,
            "can_write": not _missing(principal, write=True) if principal else False,
            "list_limit": LIST_LIMIT,
        },
    )


@router.get("/twins/slots", response_class=HTMLResponse, include_in_schema=False)
async def twins_slots_page(
    request: Request, hil_token: str = Cookie(default=""), kind: str = "device"
) -> Response:
    _, denied = await _guard(request, hil_token)
    if denied is not None:
        return denied
    if kind not in twins.SLOT_TABLES:
        return _message(
            request,
            hil_token,
            422,
            "Unknown slot kind",
            f"Slot kind must be one of: {', '.join(SLOT_KINDS)}.",
        )
    data = await _slots(request.app.state.db_path, kind)
    return _tr(
        request,
        "twins_slots.html",
        {
            "token": hil_token,
            "active": "twins",
            "title": "Physical twins — slots",
            "kind": kind,
            "slot_kinds": SLOT_KINDS,
            "slot_link": _slot_link,
            **data,
        },
    )


@router.post("/twins/observations/preview", response_class=HTMLResponse, include_in_schema=False)
async def twins_preview(
    request: Request,
    hil_token: str = Cookie(default=""),
    observations_json: Annotated[str, Form()] = "",
    file: Annotated[UploadFile | None, File()] = None,
    adopt_index: Annotated[str, Form()] = "",
    adopt_twin_id: Annotated[str, Form()] = "",
) -> Response:
    principal, denied = await _guard(request, hil_token)
    if denied is not None or principal is None:
        return denied or _login_redirect()
    try:
        text = await _read_input(observations_json, file)
        if adopt_index and adopt_twin_id:
            # Operator chose an explicit existing twin for a candidate match.
            documents = _documents(text)
            index = int(adopt_index)
            if not 0 <= index < len(documents) or not isinstance(documents[index], dict):
                raise InputProblem(422, "Adopt target is not an observation in this batch")
            documents[index]["twin_id"] = adopt_twin_id
            text = json.dumps(documents, indent=2)
    except InputProblem as problem:
        return _preview_page(request, hil_token, text=observations_json, problem=problem)
    except ValueError:
        return _preview_page(
            request,
            hil_token,
            text=observations_json,
            problem=InputProblem(422, "Adopt index must be an integer"),
        )
    return await _preview_text(request, hil_token, principal, text)


@router.post("/twins/observations/apply", response_class=HTMLResponse, include_in_schema=False)
async def twins_apply(
    request: Request,
    hil_token: str = Cookie(default=""),
    observations_json: Annotated[str, Form()] = "",
) -> Response:
    principal, denied = await _guard(request, hil_token, write=True)
    if denied is not None or principal is None:
        return denied or _login_redirect()
    try:
        observations = _validate(_documents(observations_json))
    except InputProblem as problem:
        return _preview_page(request, hil_token, text=observations_json, problem=problem)
    db_path = request.app.state.db_path
    try:
        applied = await _apply(db_path, observations, principal.subject)
    except twins.TwinError as exc:
        # Nothing was written; re-preview so the operator sees the current conflict.
        results = await _preview(db_path, observations, principal.subject)
        normalized = _normalized(observations)
        return _preview_page(
            request,
            hil_token,
            text=normalized,
            results=results,
            normalized=normalized,
            apply_error=exc,
        )
    for item, observation in zip(applied, observations, strict=True):
        item["action"] = _action(observation, item)
    return _preview_page(request, hil_token, text="", applied=applied)


@router.post(
    "/twins/collect/bridge/{host_id}", response_class=HTMLResponse, include_in_schema=False
)
async def twins_collect_bridge(
    host_id: str, request: Request, hil_token: str = Cookie(default="")
) -> Response:
    # Write capability: collection spends the controller's bridge credential.
    principal, denied = await _guard(request, hil_token, write=True)
    if denied is not None or principal is None:
        return denied or _login_redirect()
    async with get_db(request.app.state.db_path) as db:
        found = await twins.rows(
            db, "SELECT id, addr, transport, api_url, token_env FROM hosts WHERE id=?", (host_id,)
        )
    if not found:
        return _message(request, hil_token, 404, "Host not found", f"No host {host_id!r}.")
    host = found[0]
    if host["transport"] != BRIDGE_TRANSPORT:
        return _message(
            request,
            hil_token,
            422,
            "Not an esp-usbip-bridge host",
            "Bridge collection only applies to hosts with transport esp-usbip-bridge. "
            "Linux hosts are collected on the host itself (see the command on the twins page).",
        )
    origin = f"Collected from esp-usbip-bridge host {host_id} (GET /api/usb/devices)."
    env = host.get("token_env")
    if env and not os.environ.get(env):
        origin += (
            f" Token variable {env} is not set in the controller environment; "
            "the request was sent without a bearer token."
        )
    try:
        observations = await run_in_threadpool(_collect_from_bridge, host)
    except ValidationError:
        problem = InputProblem(
            502, "Bridge inventory does not satisfy the observation schema (collection failed)."
        )
        return _preview_page(request, hil_token, text="", problem=problem, origin=origin)
    except (CollectionError, ValueError) as exc:
        # CollectionError messages never include URLs, tokens or response bodies.
        message = str(exc) if isinstance(exc, CollectionError) else "invalid bridge address"
        problem = InputProblem(502, f"Bridge collection failed: {message}")
        return _preview_page(request, hil_token, text="", problem=problem, origin=origin)
    if not observations:
        problem = InputProblem(200, "The bridge reported no USB devices; nothing to preview.")
        return _preview_page(request, hil_token, text="", problem=problem, origin=origin)
    text = _normalized(observations)
    return await _preview_text(request, hil_token, principal, text, origin=origin)


@router.get("/twins/{twin_id}", response_class=HTMLResponse, include_in_schema=False)
async def twin_detail_page(
    twin_id: str,
    request: Request,
    hil_token: str = Cookie(default=""),
    closed: str = "",
) -> Response:
    principal, denied = await _guard(request, hil_token)
    if denied is not None or principal is None:
        return denied or _login_redirect()
    return await _detail_response(
        request,
        hil_token,
        principal,
        twin_id,
        notice=f"Connection {closed} closed." if closed else None,
    )


async def _detail_response(
    request: Request,
    hil_token: str,
    principal: Principal,
    twin_id: str,
    *,
    notice: str | None = None,
    error: str | None = None,
    error_status: int = 200,
) -> HTMLResponse:
    async with get_db(request.app.state.db_path) as db:
        try:
            twin = await twins.detail(db, twin_id)
        except twins.TwinError as exc:
            return _message(request, hil_token, exc.status, "Physical twin not found", exc.detail)
        others = {
            r["id"]: r["current_label"]
            for r in await twins.rows(
                db, f"SELECT t.id, {_CURRENT_LABEL} AS current_label FROM physical_twins t"
            )
        }
    for observation in twin["observations"]:
        observation["pretty"] = json.dumps(observation["payload"], indent=2, sort_keys=True)
    twin["observations"].reverse()  # newest first
    for port in twin["ports"]:
        port["attributes_pretty"] = json.dumps(port["attributes"], sort_keys=True)
    for connection in twin["connections"]:
        outgoing = connection["from_twin_id"] == twin_id
        connection["direction"] = "out" if outgoing else "in"
        connection["local_port"] = connection["from_port" if outgoing else "to_port"]
        connection["peer_id"] = connection["to_twin_id" if outgoing else "from_twin_id"]
        connection["peer_port"] = connection["to_port" if outgoing else "from_port"]
        connection["peer_label"] = others.get(connection["peer_id"])
        connection["attributes_pretty"] = json.dumps(connection["attributes"], sort_keys=True)
    open_connections = [c for c in twin["connections"] if c["ended_at"] is None]
    closed_connections = [c for c in twin["connections"] if c["ended_at"] is not None]
    for slot in twin["slot_history"]:
        slot["link"] = _slot_link(slot["slot_kind"], slot["slot_id"])
    return _tr(
        request,
        "twins_detail.html",
        {
            "token": hil_token,
            "active": "twins",
            "title": f"Physical twin {twin['label']}",
            "twin": twin,
            "attributes_pretty": json.dumps(twin["attributes"], indent=2, sort_keys=True),
            "open_connections": open_connections,
            "closed_connections": closed_connections,
            "current_slots": [s for s in twin["slot_history"] if s["ended_at"] is None],
            "can_write": not _missing(principal, write=True),
            "now": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "notice": notice,
            "error": error,
        },
        status_code=error_status,
    )


@router.post(
    "/twins/{twin_id}/connections/{connection_id}/close",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def twin_close_connection(
    twin_id: str,
    connection_id: str,
    request: Request,
    hil_token: str = Cookie(default=""),
    ended_at: Annotated[str, Form()] = "",
    source: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
) -> Response:
    principal, denied = await _guard(request, hil_token, write=True)
    if denied is not None or principal is None:
        return denied or _login_redirect()

    async def fail(status: int, detail: str) -> HTMLResponse:
        return await _detail_response(
            request,
            hil_token,
            principal,
            twin_id,
            error=f"Close connection {connection_id} failed (HTTP {status}): {detail}",
            error_status=status,
        )

    try:
        item = CloseConnection.model_validate(
            {"ended_at": ended_at.strip(), "source": source.strip(), "reason": reason.strip()}
        )
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
            for e in exc.errors(include_input=False, include_url=False)
        )
        return await fail(422, problems)
    # Mirror api/twins.py close_connection exactly (validation + idempotent repeat).
    async with get_db(request.app.state.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            existing = await twins.rows(
                db, "SELECT * FROM twin_connections WHERE id=?", (connection_id,)
            )
            if not existing or twin_id not in (
                existing[0]["from_twin_id"],
                existing[0]["to_twin_id"],
            ):
                await db.rollback()
                return await fail(404, "Connection not found")
            connection = existing[0]
            at = twins.iso(item.ended_at)
            if at < connection["started_at"]:
                await db.rollback()
                return await fail(422, "Connection cannot end before it starts")
            if connection["ended_at"] is not None and (
                connection["ended_at"] != at
                or connection["close_source"] != item.source
                or connection["close_reason"] != item.reason
            ):
                await db.rollback()
                return await fail(409, "Connection is already closed with different evidence")
            await db.execute(
                "UPDATE twin_connections SET ended_at=?,close_source=?,close_reason=?,"
                "close_actor=? WHERE id=?",
                (at, item.source, item.reason, principal.subject, connection_id),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    return RedirectResponse(
        f"/ui/twins/{quote(twin_id, safe='')}?closed={quote(connection_id, safe='')}",
        status_code=303,
    )
