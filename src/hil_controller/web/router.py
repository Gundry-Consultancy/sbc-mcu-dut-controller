"""Jinja2 / HTMX web interface for HIL controller admin."""

from __future__ import annotations

import html
import json
import logging
import shlex
import shutil
import uuid
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Cookie, File, Form, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from hil_controller import host_hardware
from hil_controller.auth.principal import Principal
from hil_controller.db.connection import get_db
from hil_controller.topology.rename import rename_device, rename_host

log = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
templates.env.filters["tojson"] = json.dumps

from hil_controller.adapters.camera import roi_snapshot
from hil_controller.adapters.usb_scan import split_dev_path as _split_dev_path

templates.env.filters["dev_path_segments"] = _split_dev_path

router = APIRouter(prefix="/ui", tags=["web"])


def _tr(request: Request, name: str, ctx: dict | None = None, **kwargs):
    """Shorthand for Starlette 1.0+ TemplateResponse(request, name, context)."""
    return templates.TemplateResponse(request, name, ctx or {}, **kwargs)


def _redirect(path: str) -> Response:
    """HX-Redirect triggers a full client navigation in HTMX, avoiding tbody nesting bugs."""
    return Response(status_code=200, headers={"HX-Redirect": path})


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


async def _check_web_token(request: Request, token: str) -> Principal | None:
    if not token:
        return None
    from fastapi.security import HTTPAuthorizationCredentials

    from hil_controller.auth.tokens import require_auth

    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    try:
        return await require_auth(request, creds)
    except Exception:
        return None


def _login_redirect() -> RedirectResponse:
    return RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def _hosts(db_path: str) -> list[dict]:
    async with get_db(db_path) as db:
        async with db.execute(
            """
            SELECT h.*, COUNT(d.id) AS device_count
            FROM hosts h LEFT JOIN devices d ON d.host_id = h.id
            GROUP BY h.id ORDER BY h.id
            """
        ) as cur:
            rows = await cur.fetchall()
    return [
        {
            **dict(r),
            "capabilities": json.loads(r["capabilities_json"]),
            "hw": host_hardware.host_hw_view(dict(r)),
        }
        for r in rows
    ]


async def _devices(db_path: str) -> list[dict]:
    async with get_db(db_path) as db:
        async with db.execute("SELECT * FROM devices ORDER BY id") as cur:
            rows = await cur.fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["capabilities"] = json.loads(d.pop("capabilities_json"))
        usb = json.loads(d.pop("usb_json") or "null") or {}
        d["usb_vid"] = usb.get("vid", "")
        d["usb_pid"] = usb.get("pid", "")
        result.append(d)
    return result


def _parse_streams(row: dict) -> list[dict]:
    raw = row.get("streams_json")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    # fall back to legacy single interface/observability fields
    if row.get("interface"):
        return [{"url": row["interface"], "type": row.get("observability", "other")}]
    return []


async def _aux_list(db_path: str, kind_filter: str | None = None) -> list[dict]:
    async with get_db(db_path) as db:
        if kind_filter:
            async with db.execute(
                "SELECT * FROM auxes WHERE kind = ? ORDER BY id", (kind_filter,)
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute("SELECT * FROM auxes ORDER BY id") as cur:
                rows = await cur.fetchall()
        result = []
        for r in rows:
            a = dict(r)
            a["capabilities"] = json.loads(a.pop("capabilities_json"))
            a["streams"] = _parse_streams(a)
            async with db.execute("SELECT * FROM connections WHERE aux_id = ?", (a["id"],)) as ccur:
                a["connections"] = [dict(c) for c in await ccur.fetchall()]
            result.append(a)
    return result


async def _aux_by_id(db_path: str, aux_id: str) -> dict | None:
    async with get_db(db_path) as db:
        async with db.execute("SELECT * FROM auxes WHERE id = ?", (aux_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        a = dict(row)
        a["capabilities"] = json.loads(a.pop("capabilities_json"))
        a["streams"] = _parse_streams(a)
        async with db.execute("SELECT * FROM connections WHERE aux_id = ?", (aux_id,)) as ccur:
            a["connections"] = [dict(c) for c in await ccur.fetchall()]
    return a


async def _cameras(db_path: str) -> list[dict]:
    async with get_db(db_path) as db:
        async with db.execute("SELECT * FROM cameras ORDER BY id") as cur:
            rows = await cur.fetchall()
    result = []
    for r in rows:
        c = dict(r)
        c["streams"] = _parse_streams(c)
        result.append(c)
    return result


async def _peripherals_list(db_path: str) -> list[dict]:
    async with get_db(db_path) as db:
        async with db.execute("SELECT * FROM peripherals ORDER BY id") as cur:
            rows = await cur.fetchall()
        async with db.execute(
            "SELECT peripheral_id, device_id FROM device_peripherals ORDER BY peripheral_id"
        ) as cur:
            dp_rows = await cur.fetchall()
    periph_devices: dict[str, list[str]] = {}
    for dp in dp_rows:
        periph_devices.setdefault(dp["peripheral_id"], []).append(dp["device_id"])
    result = []
    for r in rows:
        p = dict(r)
        p["device_ids"] = periph_devices.get(p["id"], [])
        p["specs"] = _parse_specs(p.get("specs_json"))
        result.append(p)
    return result


def _parse_specs(specs_json: str | None) -> dict:
    """Decode a peripheral's specs_json blob to a dict (empty on missing/bad)."""
    if not specs_json:
        return {}
    try:
        return json.loads(specs_json) or {}
    except Exception:
        return {}


def _specs_summary(specs: dict) -> str:
    """One-line 'resolution · controller · interface' from a specs dict."""
    parts = [specs.get(k) for k in ("resolution", "controller", "interface")]
    return " · ".join(str(p) for p in parts if p)


async def _camera_by_id(db_path: str, cam_id: str) -> dict | None:
    async with get_db(db_path) as db:
        async with db.execute("SELECT * FROM cameras WHERE id = ?", (cam_id,)) as cur:
            row = await cur.fetchone()
    if row is None:
        return None
    c = dict(row)
    c["streams"] = _parse_streams(c)
    return c


def _parse_caps(raw: str) -> list[str]:
    return [c.strip() for c in raw.split(",") if c.strip()]


# ---------------------------------------------------------------------------
# Login / logout
# ---------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request, error: str = "") -> HTMLResponse:
    return _tr(request, "login.html", {"error": error})


@router.post("/login", include_in_schema=False, response_model=None)
async def do_login(request: Request, token: Annotated[str, Form()] = "") -> Response:
    p = await _check_web_token(request, token)
    if p is None:
        return _tr(request, "login.html", {"error": "Invalid token"}, status_code=401)
    resp = RedirectResponse("/ui/", status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie("hil_token", token, httponly=True, samesite="strict", path="/ui")
    return resp


@router.get("/logout", include_in_schema=False)
async def do_logout() -> RedirectResponse:
    resp = RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie("hil_token", path="/ui")
    return resp


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


@router.get("/empty", response_class=HTMLResponse, include_in_schema=False)
async def empty() -> HTMLResponse:
    return HTMLResponse("")


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request, hil_token: str = Cookie(default="")) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path

    hosts = await _hosts(db_path)
    devices = await _devices(db_path)
    hw = await _aux_list(db_path)
    cameras = await _cameras(db_path)

    async with get_db(db_path) as db:
        async with db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 10") as cur:
            recent_jobs = [dict(r) for r in await cur.fetchall()]
        async with db.execute(
            "SELECT COUNT(*) FROM jobs WHERE state NOT IN ('finished','error','timeout','cancelled')"  # noqa: E501
        ) as cur:
            row = await cur.fetchone()
            active_jobs = row[0] if row else 0

    from hil_controller.config import get_settings

    scripts_dir = get_settings().scripts_dir
    script_count = 0
    if scripts_dir and Path(scripts_dir).exists():
        script_count = len(list(Path(scripts_dir).glob("*.json")))

    return _tr(
        request,
        "dashboard.html",
        {
            "token": hil_token,
            "active": "dashboard",
            "hosts": hosts,
            "devices": devices,
            "hardware": hw,
            "cameras": cameras,
            "recent_jobs": recent_jobs,
            "active_jobs": active_jobs,
            "script_count": script_count,
        },
    )


# ---------------------------------------------------------------------------
# Hosts CRUD
# ---------------------------------------------------------------------------


@router.get("/hosts", response_class=HTMLResponse, include_in_schema=False)
async def hosts_page(request: Request, hil_token: str = Cookie(default="")) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    hosts = await _hosts(db_path)
    return _tr(request, "hosts.html", {"token": hil_token, "active": "hosts", "hosts": hosts})


@router.get("/hosts/form", response_class=HTMLResponse, include_in_schema=False)
async def new_host_form(request: Request, hil_token: str = Cookie(default="")) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    return _tr(request, "hosts_form.html", {"host": None})


@router.get("/hosts/{host_id}/form", response_class=HTMLResponse, include_in_schema=False)
async def edit_host_form(
    request: Request, host_id: str, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        async with db.execute("SELECT * FROM hosts WHERE id = ?", (host_id,)) as cur:
            row = await cur.fetchone()
    if row is None:
        return HTMLResponse("Host not found", status_code=404)
    h = dict(row)
    h["capabilities"] = json.loads(h.pop("capabilities_json"))
    h["hw"] = host_hardware.host_hw_view(dict(row))
    h["hw_detected"] = host_hardware._loads(row["hw_detected_json"])
    h["hw_override"] = host_hardware._loads(row["hw_override_json"])
    return _tr(request, "hosts_form.html", {"host": h})


@router.post("/hosts", response_class=HTMLResponse, include_in_schema=False)
async def create_host(
    request: Request,
    hil_token: str = Cookie(default=""),
    id: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "",
    addr: Annotated[str, Form()] = "",
    transport: Annotated[str, Form()] = "ssh",
    ssh_user: Annotated[str, Form()] = "pi",
    ssh_key_path: Annotated[str, Form()] = "",
    max_concurrent_jobs: Annotated[str, Form()] = "",
    capabilities: Annotated[str, Form()] = "",
    status: Annotated[str, Form()] = "available",
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    if not id:
        return _tr(request, "hosts_form.html", {"host": None, "error": "ID is required"})
    db_path: str = request.app.state.db_path
    max_jobs = int(max_concurrent_jobs) if max_concurrent_jobs.strip() else None
    async with get_db(db_path) as db:
        try:
            await db.execute(
                """INSERT INTO hosts
                   (id, role, addr, transport, ssh_user, ssh_key_path,
                    max_concurrent_jobs, capabilities_json, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    id,
                    role,
                    addr,
                    transport,
                    ssh_user,
                    ssh_key_path or None,
                    max_jobs,
                    json.dumps(_parse_caps(capabilities)),
                    status,
                ),
            )
            await db.commit()
        except Exception as exc:
            return _tr(request, "hosts_form.html", {"host": None, "error": str(exc)})
    return _redirect("/ui/hosts")


@router.post("/hosts/{host_id}", response_class=HTMLResponse, include_in_schema=False)
async def update_host(
    request: Request,
    host_id: str,
    hil_token: str = Cookie(default=""),
    id: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "",
    addr: Annotated[str, Form()] = "",
    transport: Annotated[str, Form()] = "ssh",
    ssh_user: Annotated[str, Form()] = "pi",
    ssh_key_path: Annotated[str, Form()] = "",
    max_concurrent_jobs: Annotated[str, Form()] = "",
    capabilities: Annotated[str, Form()] = "",
    status: Annotated[str, Form()] = "available",
    # Operator hardware overrides (blank = detected value stands). See host_hardware.
    ov_model: Annotated[str, Form()] = "",
    ov_cpu_model: Annotated[str, Form()] = "",
    ov_cpu_cores: Annotated[str, Form()] = "",
    ov_cpu_mhz: Annotated[str, Form()] = "",
    ov_mem_total_kb: Annotated[str, Form()] = "",
    ov_storage_total_kb: Annotated[str, Form()] = "",
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    max_jobs = int(max_concurrent_jobs) if max_concurrent_jobs.strip() else None
    override = _hw_override_from_form(
        ov_model, ov_cpu_model, ov_cpu_cores, ov_cpu_mhz, ov_mem_total_kb, ov_storage_total_kb
    )

    def _reshow(existing_row, error: str) -> HTMLResponse:
        h = dict(existing_row)
        h["capabilities"] = json.loads(h.pop("capabilities_json"))
        h["hw"] = host_hardware.host_hw_view(dict(existing_row))
        h["hw_detected"] = host_hardware._loads(existing_row["hw_detected_json"])
        h["hw_override"] = host_hardware._loads(existing_row["hw_override_json"])
        return _tr(request, "hosts_form.html", {"host": h, "error": error})

    async with get_db(db_path) as db:
        async with db.execute("SELECT * FROM hosts WHERE id = ?", (host_id,)) as cur:
            existing = await cur.fetchone()
        if existing is None:
            return HTMLResponse("Host not found", status_code=404)
        try:
            await db.execute(
                """UPDATE hosts SET role=?, addr=?, transport=?, ssh_user=?, ssh_key_path=?,
                   max_concurrent_jobs=?, capabilities_json=?, status=?, hw_override_json=?
                   WHERE id=?""",
                (
                    role,
                    addr,
                    transport,
                    ssh_user,
                    ssh_key_path or None,
                    max_jobs,
                    json.dumps(_parse_caps(capabilities)),
                    status,
                    json.dumps(override) if override else None,
                    host_id,
                ),
            )
            await db.commit()
            # Rename last (cascades to devices/jobs/cameras/leases) so a clash
            # leaves the field edits intact and surfaces a clear error.
            if id.strip() and id.strip() != host_id:
                await rename_host(db, host_id, id.strip())
        except (ValueError, KeyError) as exc:
            return _reshow(existing, str(exc).strip("'"))
        except Exception as exc:
            return _reshow(existing, str(exc))
    return _redirect("/ui/hosts")


def _hw_override_from_form(
    model: str,
    cpu_model: str,
    cpu_cores: str,
    cpu_mhz: str,
    mem_total_kb: str,
    storage_total_kb: str,
) -> dict:
    """Collect non-blank operator hardware overrides into a specs-shaped dict."""
    out: dict = {}
    if model.strip():
        out["model"] = model.strip()
    if cpu_model.strip():
        out["cpu_model"] = cpu_model.strip()
    for key, raw, conv in (
        ("cpu_cores", cpu_cores, int),
        ("cpu_mhz", cpu_mhz, float),
        ("mem_total_kb", mem_total_kb, int),
        ("storage_total_kb", storage_total_kb, int),
    ):
        if raw.strip():
            try:
                out[key] = conv(raw.strip())
            except ValueError:
                pass
    return out


async def _render_host_form(request: Request, host_id: str, error: str = "") -> HTMLResponse:
    """Re-render the host edit form from the DB (used after a manual hw refresh)."""
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        async with db.execute("SELECT * FROM hosts WHERE id = ?", (host_id,)) as cur:
            row = await cur.fetchone()
    if row is None:
        return HTMLResponse("Host not found", status_code=404)
    h = dict(row)
    h["capabilities"] = json.loads(h.pop("capabilities_json"))
    h["hw"] = host_hardware.host_hw_view(dict(row))
    h["hw_detected"] = host_hardware._loads(row["hw_detected_json"])
    h["hw_override"] = host_hardware._loads(row["hw_override_json"])
    return _tr(request, "hosts_form.html", {"host": h, "error": error})


def _host_transport(request: Request, host_id: str):
    """Resolve a transport for a host, or return (None, error_message)."""
    registry = getattr(request.app.state, "host_registry", None)
    if registry is None:
        return None, "host registry not configured on this controller"
    try:
        return registry.transport_for(host_id), ""
    except (KeyError, AttributeError, ValueError) as exc:
        return None, f"no transport for host: {exc}"


@router.post("/hosts/{host_id}/refresh-specs", response_class=HTMLResponse, include_in_schema=False)
async def refresh_host_specs(
    request: Request, host_id: str, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    transport, err = _host_transport(request, host_id)
    if transport is None:
        return await _render_host_form(request, host_id, err)
    try:
        specs = await host_hardware.probe_specs(transport)
        await host_hardware.store_specs(request.app.state.db_path, host_id, specs)
    except Exception as exc:  # noqa: BLE001 — surface the probe failure in the form
        return await _render_host_form(request, host_id, f"spec probe failed: {exc}")
    return await _render_host_form(request, host_id)


@router.post("/hosts/{host_id}/refresh-load", response_class=HTMLResponse, include_in_schema=False)
async def refresh_host_load(
    request: Request, host_id: str, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    transport, err = _host_transport(request, host_id)
    if transport is None:
        return await _render_host_form(request, host_id, err)
    try:
        load = await host_hardware.probe_load(transport)
        await host_hardware.store_load(request.app.state.db_path, host_id, load)
    except Exception as exc:  # noqa: BLE001
        return await _render_host_form(request, host_id, f"load probe failed: {exc}")
    return await _render_host_form(request, host_id)


@router.post("/hosts/{host_id}/benchmark", response_class=HTMLResponse, include_in_schema=False)
async def benchmark_host(
    request: Request, host_id: str, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    transport, err = _host_transport(request, host_id)
    if transport is None:
        return await _render_host_form(request, host_id, err)
    from hil_controller.config import get_settings

    cfg = get_settings()
    try:
        result = await host_hardware.benchmark_speed(
            transport,
            baseline_openssl=cfg.speed_baseline_openssl,
            baseline_sysbench=cfg.speed_baseline_sysbench,
        )
        await host_hardware.store_speed_score(
            request.app.state.db_path, host_id, result.get("score")
        )
    except Exception as exc:  # noqa: BLE001
        return await _render_host_form(request, host_id, f"benchmark failed: {exc}")
    return await _render_host_form(request, host_id)


@router.delete("/hosts/{host_id}", response_class=HTMLResponse, include_in_schema=False)
async def delete_host(
    request: Request, host_id: str, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        await db.execute("DELETE FROM hosts WHERE id = ?", (host_id,))
        await db.commit()
    return HTMLResponse("")


# ---------------------------------------------------------------------------
# Devices CRUD
# ---------------------------------------------------------------------------


@router.get("/devices", response_class=HTMLResponse, include_in_schema=False)
async def devices_page(request: Request, hil_token: str = Cookie(default="")) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    devices = await _devices(db_path)
    return _tr(
        request, "devices.html", {"token": hil_token, "active": "devices", "devices": devices}
    )


@router.get("/devices/form", response_class=HTMLResponse, include_in_schema=False)
async def new_device_form(request: Request, hil_token: str = Cookie(default="")) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    hosts = await _hosts(db_path)
    cameras = await _cameras(db_path)
    # token is REQUIRED so the form's "Discover busids" button can call the API
    # (the JS sends `Bearer {{ token }}`); without it the call 401s "Missing token".
    return _tr(
        request,
        "devices_form.html",
        {"device": None, "hosts": hosts, "cameras": cameras, "token": hil_token},
    )


@router.get("/devices/{device_id}/form", response_class=HTMLResponse, include_in_schema=False)
async def edit_device_form(
    request: Request, device_id: str, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        async with db.execute("SELECT * FROM devices WHERE id = ?", (device_id,)) as cur:
            row = await cur.fetchone()
        async with db.execute("SELECT * FROM camera_rois WHERE device_id = ?", (device_id,)) as cur:
            roi_row = await cur.fetchone()
    if row is None:
        return HTMLResponse("Device not found", status_code=404)
    d = dict(row)
    d["capabilities"] = json.loads(d.pop("capabilities_json"))
    usb = json.loads(d.pop("usb_json") or "null") or {}
    d["usb_vid"] = usb.get("vid", "")
    d["usb_pid"] = usb.get("pid", "")
    hosts = await _hosts(db_path)
    cameras = await _cameras(db_path)
    roi = dict(roi_row) if roi_row else None
    return _tr(
        request,
        "devices_form.html",
        {"device": d, "hosts": hosts, "cameras": cameras, "token": hil_token, "roi": roi},
    )


@router.get("/devices/{device_id}/snapshot", include_in_schema=False)
async def device_snapshot_proxy(
    request: Request, device_id: str, hil_token: str = Cookie(default="")
) -> Response:
    """Cookie-authed JPEG proxy for the device camera panel img src."""
    if not (await _check_web_token(request, hil_token)):
        return Response(status_code=401)
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        async with db.execute(
            "SELECT d.camera_id, r.x, r.y, r.w, r.h, "
            "r.roi_frame_width, r.roi_frame_height, c.source, c.streams_json "
            "FROM devices d "
            "LEFT JOIN camera_rois r ON r.device_id = d.id "
            "LEFT JOIN cameras c ON c.id = d.camera_id "
            "WHERE d.id = ?",
            (device_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None or not row["camera_id"]:
            return Response(status_code=404)

        streams = json.loads(row["streams_json"]) if row["streams_json"] else []
        if not streams and row["source"]:
            streams = [{"url": row["source"], "type": "snapshot"}]
        warm_url = next(
            (s["url"] for s in streams if s.get("type") in ("snapshot", "mjpeg")),
            row["source"] or "",
        )
        full = request.query_params.get("res") == "full"
        ref_w, ref_h = row["roi_frame_width"], row["roi_frame_height"]
        url = (roi_snapshot.full_res_url(row["source"], streams) or warm_url) if full else warm_url
        if not url:
            return Response(status_code=503)

        # Backfill the reference frame for legacy ROIs so full-res scaling works.
        if full and row["x"] is not None and not (ref_w and ref_h) and warm_url:
            try:
                import httpx

                async with httpx.AsyncClient(timeout=10.0) as client:
                    wr = await client.get(warm_url)
                    wr.raise_for_status()
                dims = roi_snapshot.decode_dims(wr.content)
            except Exception:
                dims = None
            if dims:
                ref_w, ref_h = dims
                await db.execute(
                    "UPDATE camera_rois SET roi_frame_width=?, roi_frame_height=? WHERE device_id=?",  # noqa: E501
                    (ref_w, ref_h, device_id),
                )
                await db.commit()

    try:
        import httpx

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            frame_bytes = resp.content
    except Exception:
        return Response(status_code=503)

    if row["x"] is not None and request.query_params.get("crop") == "1":
        crop = roi_snapshot.crop_to_jpeg(
            frame_bytes,
            x=int(row["x"]),
            y=int(row["y"]),
            w=int(row["w"]),
            h=int(row["h"]),
            ref_w=ref_w if full else None,
            ref_h=ref_h if full else None,
        )
        if crop is not None:
            return Response(content=crop, media_type="image/jpeg")

    return Response(content=frame_bytes, media_type="image/jpeg")


def _parse_optional_float(s: str) -> float | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_optional_int(s: str) -> int | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


@router.post("/devices", response_class=HTMLResponse, include_in_schema=False)
async def create_device(
    request: Request,
    hil_token: str = Cookie(default=""),
    id: Annotated[str, Form()] = "",
    host_id: Annotated[str, Form()] = "",
    kind: Annotated[str, Form()] = "microcontroller",
    model: Annotated[str, Form()] = "",
    pool: Annotated[str, Form()] = "public",
    capabilities: Annotated[str, Form()] = "",
    serial_port: Annotated[str, Form()] = "",
    flasher: Annotated[str, Form()] = "",
    usb_vid: Annotated[str, Form()] = "",
    usb_pid: Annotated[str, Form()] = "",
    status: Annotated[str, Form()] = "available",
    camera_id: Annotated[str, Form()] = "",
    qr_identifier: Annotated[str, Form()] = "",
    manual_focus: Annotated[str, Form()] = "",
    illuminator_brightness: Annotated[str, Form()] = "",
    hub_host_id: Annotated[str, Form()] = "",
    hub_port_path: Annotated[str, Form()] = "",
    solenoid_channel: Annotated[str, Form()] = "",
    usb_serial: Annotated[str, Form()] = "",
    bootsel_channel: Annotated[str, Form()] = "",
    bootsel_inverted: Annotated[str, Form()] = "",
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    if not id or not host_id:
        hosts = await _hosts(db_path)
        cameras = await _cameras(db_path)
        return _tr(
            request,
            "devices_form.html",
            {
                "device": None,
                "hosts": hosts,
                "cameras": cameras,
                "token": hil_token,
                "error": "ID and Host are required",
            },
        )
    usb_json = json.dumps({"vid": usb_vid, "pid": usb_pid}) if (usb_vid or usb_pid) else None
    focus_val = _parse_optional_float(manual_focus)
    brightness_val = _parse_optional_int(illuminator_brightness)
    solenoid_val = _parse_optional_int(solenoid_channel)
    bootsel_val = _parse_optional_int(bootsel_channel)
    bootsel_inv = 1 if bootsel_inverted else 0
    hub_host_val = hub_host_id or host_id  # default to device host
    async with get_db(db_path) as db:
        try:
            await db.execute(
                """INSERT INTO devices
                   (id, host_id, kind, model, capabilities_json, usb_json,
                    pool, status, serial_port, flasher, camera_id, qr_identifier,
                    manual_focus, illuminator_brightness,
                    hub_host_id, hub_port_path, solenoid_channel, usb_serial,
                    bootsel_channel, bootsel_inverted)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    id,
                    host_id,
                    kind,
                    model,
                    json.dumps(_parse_caps(capabilities)),
                    usb_json,
                    pool,
                    status,
                    serial_port or None,
                    flasher or None,
                    camera_id or None,
                    qr_identifier or None,
                    focus_val,
                    brightness_val,
                    hub_host_val,
                    hub_port_path or None,
                    solenoid_val,
                    usb_serial or None,
                    bootsel_val,
                    bootsel_inv,
                ),
            )
            await db.commit()
        except Exception as exc:
            hosts = await _hosts(db_path)
            cameras = await _cameras(db_path)
            return _tr(
                request,
                "devices_form.html",
                {
                    "device": None,
                    "hosts": hosts,
                    "cameras": cameras,
                    "token": hil_token,
                    "error": str(exc),
                },
            )
    # Push the new device's settings to its camera (best-effort, no-op when no camera).
    if camera_id:
        try:
            from hil_controller.adapters.camera.orchestrator import recompute_for_camera

            async with get_db(db_path) as db:
                await recompute_for_camera(db, camera_id)
        except Exception:
            pass
    return _redirect("/ui/devices")


@router.post("/devices/{device_id}", response_class=HTMLResponse, include_in_schema=False)
async def update_device(
    request: Request,
    device_id: str,
    hil_token: str = Cookie(default=""),
    id: Annotated[str, Form()] = "",
    host_id: Annotated[str, Form()] = "",
    kind: Annotated[str, Form()] = "microcontroller",
    model: Annotated[str, Form()] = "",
    pool: Annotated[str, Form()] = "public",
    capabilities: Annotated[str, Form()] = "",
    serial_port: Annotated[str, Form()] = "",
    flasher: Annotated[str, Form()] = "",
    usb_vid: Annotated[str, Form()] = "",
    usb_pid: Annotated[str, Form()] = "",
    status: Annotated[str, Form()] = "available",
    camera_id: Annotated[str, Form()] = "",
    qr_identifier: Annotated[str, Form()] = "",
    manual_focus: Annotated[str, Form()] = "",
    illuminator_brightness: Annotated[str, Form()] = "",
    hub_host_id: Annotated[str, Form()] = "",
    hub_port_path: Annotated[str, Form()] = "",
    solenoid_channel: Annotated[str, Form()] = "",
    usb_serial: Annotated[str, Form()] = "",
    bootsel_channel: Annotated[str, Form()] = "",
    bootsel_inverted: Annotated[str, Form()] = "",
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    usb_json = json.dumps({"vid": usb_vid, "pid": usb_pid}) if (usb_vid or usb_pid) else None
    focus_val = _parse_optional_float(manual_focus)
    brightness_val = _parse_optional_int(illuminator_brightness)
    solenoid_val = _parse_optional_int(solenoid_channel)
    bootsel_val = _parse_optional_int(bootsel_channel)
    bootsel_inv = 1 if bootsel_inverted else 0
    hub_host_val = hub_host_id or host_id
    async with get_db(db_path) as db:
        async with db.execute("SELECT id FROM devices WHERE id = ?", (device_id,)) as cur:
            if await cur.fetchone() is None:
                return HTMLResponse("Device not found", status_code=404)
        await db.execute(
            """UPDATE devices SET host_id=?, kind=?, model=?, capabilities_json=?,
               usb_json=?, pool=?, status=?, serial_port=?, flasher=?,
               camera_id=?, qr_identifier=?,
               manual_focus=?, illuminator_brightness=?,
               hub_host_id=?, hub_port_path=?, solenoid_channel=?, usb_serial=?,
               bootsel_channel=?, bootsel_inverted=?
               WHERE id=?""",
            (
                host_id,
                kind,
                model,
                json.dumps(_parse_caps(capabilities)),
                usb_json,
                pool,
                status,
                serial_port or None,
                flasher or None,
                camera_id or None,
                qr_identifier or None,
                focus_val,
                brightness_val,
                hub_host_val,
                hub_port_path or None,
                solenoid_val,
                usb_serial or None,
                bootsel_val,
                bootsel_inv,
                device_id,
            ),
        )
        await db.commit()
        # Rename last (cascades to usb-ids/leases/connections/rois/peripherals/jobs).
        if id.strip() and id.strip() != device_id:
            try:
                await rename_device(db, device_id, id.strip())
            except (ValueError, KeyError) as exc:
                hosts = await _hosts(db_path)
                cameras = await _cameras(db_path)
                async with db.execute("SELECT * FROM devices WHERE id = ?", (device_id,)) as cur:
                    row = await cur.fetchone()
                d = dict(row) if row else {"id": device_id}
                if row is not None:
                    d["capabilities"] = json.loads(d.pop("capabilities_json", "[]"))
                return _tr(
                    request,
                    "devices_form.html",
                    {
                        "device": d,
                        "hosts": hosts,
                        "cameras": cameras,
                        "token": hil_token,
                        "error": str(exc).strip("'"),
                    },
                )
    if camera_id:
        try:
            from hil_controller.adapters.camera.orchestrator import recompute_for_camera

            async with get_db(db_path) as db:
                await recompute_for_camera(db, camera_id)
        except Exception:
            pass
    return _redirect("/ui/devices")


@router.delete("/devices/{device_id}", response_class=HTMLResponse, include_in_schema=False)
async def delete_device(
    request: Request, device_id: str, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        await db.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        await db.commit()
    return HTMLResponse("")


# ---------------------------------------------------------------------------
# Device USB IDs — HTMX partials
# ---------------------------------------------------------------------------


async def _usb_ids_for(db_path: str, device_id: str) -> list[dict]:
    async with get_db(db_path) as db:
        async with db.execute(
            "SELECT * FROM device_usb_ids WHERE device_id = ? ORDER BY id",
            (device_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


def _render_usb_ids(
    request: Request, device_id: str, rows: list[dict], error: str = ""
) -> HTMLResponse:
    return _tr(
        request,
        "usb_ids_list.html",
        {"device_id": device_id, "rows": rows, "error": error},
    )


@router.get("/devices/{device_id}/usb-ids", response_class=HTMLResponse, include_in_schema=False)
async def ui_list_device_usb_ids(
    request: Request, device_id: str, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    rows = await _usb_ids_for(db_path, device_id)
    return _render_usb_ids(request, device_id, rows)


@router.post("/devices/{device_id}/usb-ids", response_class=HTMLResponse, include_in_schema=False)
async def ui_add_device_usb_id(
    request: Request,
    device_id: str,
    hil_token: str = Cookie(default=""),
    vid: Annotated[str, Form()] = "",
    pid: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "unknown",
    description: Annotated[str, Form()] = "",
    iserial: Annotated[str, Form()] = "",
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    error = ""
    vid_n = (vid or "").strip().lower()
    pid_n = (pid or "").strip().lower()
    if not vid_n or not pid_n:
        error = "VID and PID are required"
    else:
        now = datetime.now(UTC).isoformat()
        async with get_db(db_path) as db:
            async with db.execute("SELECT 1 FROM devices WHERE id=?", (device_id,)) as cur:
                if await cur.fetchone() is None:
                    return HTMLResponse("Device not found", status_code=404)
            try:
                await db.execute(
                    "INSERT INTO device_usb_ids "
                    "(device_id, vid, pid, role, iserial, description, "
                    " first_seen_at, last_seen_at, source) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'manual')",
                    (
                        device_id,
                        vid_n,
                        pid_n,
                        role or "unknown",
                        iserial or None,
                        description or None,
                        now,
                        now,
                    ),
                )
                await db.commit()
            except Exception as exc:
                error = f"could not add: {exc}"
    rows = await _usb_ids_for(db_path, device_id)
    return _render_usb_ids(request, device_id, rows, error=error)


@router.delete(
    "/devices/{device_id}/usb-ids/{row_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def ui_delete_device_usb_id(
    request: Request, device_id: str, row_id: int, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        await db.execute(
            "DELETE FROM device_usb_ids WHERE id = ? AND device_id = ?",
            (row_id, device_id),
        )
        await db.commit()
    rows = await _usb_ids_for(db_path, device_id)
    return _render_usb_ids(request, device_id, rows)


@router.post(
    "/devices/{device_id}/learn-usb",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def ui_learn_usb(
    request: Request,
    device_id: str,
    hil_token: str = Cookie(default=""),
    include_reset_cycle: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """HTMX endpoint: run UsbFingerprintAdapter.learn and refresh the panel."""
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path

    from hil_controller.adapters.usb_fingerprint import (
        FingerprintError,
        UsbFingerprintAdapter,
    )
    from hil_controller.queue.leases import LeaseConflict

    error = ""
    provider = getattr(request.app.state, "usb_fingerprint_provider", None)
    try:
        if provider is None:
            adapter = UsbFingerprintAdapter(
                db_path=db_path,
                hub=_LearnNoopHub(),
                scan_fn=lambda: [],
            )
        else:
            adapter = provider(db_path=db_path)
        await adapter.learn(
            device_id=device_id,
            job_id=None,
            include_reset_cycle=bool(include_reset_cycle),
        )
    except FingerprintError as exc:
        error = f"{exc}"
    except LeaseConflict as exc:
        error = f"hub busy: {exc}"
    except Exception as exc:
        error = f"learn failed: {exc}"

    rows = await _usb_ids_for(db_path, device_id)
    return _render_usb_ids(request, device_id, rows, error=error)


class _LearnNoopHub:
    async def all_off(self) -> None:
        pass

    async def port_on(self, channel: int) -> None:
        pass

    async def port_off(self, channel: int, **kwargs) -> None:
        pass


@router.post("/devices/{device_id}/camera/preview", include_in_schema=False)
async def preview_camera_settings(
    request: Request, device_id: str, hil_token: str = Cookie(default="")
) -> JSONResponse:
    """Bypass the compromise and push form values directly to the camera.

    Used by the device edit form's Preview button so the operator can see
    the effect of a candidate focus/brightness before saving — without
    needing a running job on the device.
    """
    if not (await _check_web_token(request, hil_token)):
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    raw_focus = body.get("focus")
    raw_brightness = body.get("brightness")
    focus = float(raw_focus) if raw_focus not in (None, "") else None
    brightness = int(raw_brightness) if raw_brightness not in (None, "") else None

    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        async with db.execute("SELECT camera_id FROM devices WHERE id = ?", (device_id,)) as cur:
            row = await cur.fetchone()
        if row is None or not row["camera_id"]:
            return JSONResponse({"error": "device has no camera"}, status_code=400)
        async with db.execute(
            "SELECT source, kind FROM cameras WHERE id = ?", (row["camera_id"],)
        ) as cur:
            cam_row = await cur.fetchone()
        if cam_row is None:
            return JSONResponse({"error": "camera not found"}, status_code=404)

    import httpx

    from hil_controller.adapters.camera.focus_drivers import get_driver, resolve_camera_kind
    from hil_controller.adapters.camera.orchestrator import camera_base_url

    base = camera_base_url(cam_row["source"])
    if base is None:
        return JSONResponse({"error": "camera source is not HTTP"}, status_code=400)

    # Preview pushes a raw value, bypassing the shared-camera compromise: a focus
    # value means manual focus, no value means continuous auto.
    directive = (
        {"mode": "manual", "window": None, "position": focus, "target_device": device_id}
        if focus is not None
        else {"mode": "auto", "window": None, "position": None, "target_device": device_id}
    )
    driver = get_driver(resolve_camera_kind(cam_row))
    async with httpx.AsyncClient(timeout=3.0) as client:
        await driver.apply(client, base, directive)
        await driver.apply_illuminator(client, base, brightness)
    return JSONResponse({"ok": True, "base": base, "focus": focus, "brightness": brightness})


# ---------------------------------------------------------------------------
# Hardware / Aux CRUD
# ---------------------------------------------------------------------------


@router.get("/hardware", response_class=HTMLResponse, include_in_schema=False)
async def hardware_page(request: Request, hil_token: str = Cookie(default="")) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    hardware = await _aux_list(db_path)
    peripherals = await _peripherals_list(db_path)
    strands = await _strands_web_list(db_path)
    return _tr(
        request,
        "hardware.html",
        {
            "token": hil_token,
            "active": "hardware",
            "hardware": hardware,
            "peripherals": peripherals,
            "strands": strands,
        },
    )


@router.get("/hardware/form", response_class=HTMLResponse, include_in_schema=False)
async def new_hardware_form(request: Request, hil_token: str = Cookie(default="")) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    devices = await _devices(db_path)
    return _tr(request, "hardware_form.html", {"aux": None, "devices": devices})


@router.get("/hardware/{aux_id}/form", response_class=HTMLResponse, include_in_schema=False)
async def edit_hardware_form(
    request: Request, aux_id: str, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    aux = await _aux_by_id(db_path, aux_id)
    if aux is None:
        return HTMLResponse("Aux not found", status_code=404)
    devices = await _devices(db_path)
    return _tr(request, "hardware_form.html", {"aux": aux, "devices": devices})


@router.post("/hardware", response_class=HTMLResponse, include_in_schema=False)
async def create_hardware(
    request: Request,
    hil_token: str = Cookie(default=""),
    id: Annotated[str, Form()] = "",
    kind: Annotated[str, Form()] = "display",
    model: Annotated[str, Form()] = "",
    interface: Annotated[str, Form()] = "",
    observability: Annotated[str, Form()] = "none",
    capabilities: Annotated[str, Form()] = "",
    pool: Annotated[str, Form()] = "public",
    status: Annotated[str, Form()] = "available",
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    if not id:
        devices = await _devices(db_path)
        return _tr(
            request,
            "hardware_form.html",
            {"aux": None, "devices": devices, "error": "ID is required"},
        )
    async with get_db(db_path) as db:
        try:
            await db.execute(
                """INSERT INTO auxes
                   (id, kind, model, capabilities_json, interface, observability, pool, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    id,
                    kind,
                    model,
                    json.dumps(_parse_caps(capabilities)),
                    interface,
                    observability,
                    pool,
                    status,
                ),
            )
            await db.commit()
        except Exception as exc:
            devices = await _devices(db_path)
            return _tr(
                request, "hardware_form.html", {"aux": None, "devices": devices, "error": str(exc)}
            )
    return _redirect("/ui/hardware")


@router.post("/hardware/{aux_id}", response_class=HTMLResponse, include_in_schema=False)
async def update_hardware(
    request: Request,
    aux_id: str,
    hil_token: str = Cookie(default=""),
    kind: Annotated[str, Form()] = "display",
    model: Annotated[str, Form()] = "",
    interface: Annotated[str, Form()] = "",
    observability: Annotated[str, Form()] = "none",
    capabilities: Annotated[str, Form()] = "",
    pool: Annotated[str, Form()] = "public",
    status: Annotated[str, Form()] = "available",
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        async with db.execute("SELECT id FROM auxes WHERE id = ?", (aux_id,)) as cur:
            if await cur.fetchone() is None:
                return HTMLResponse("Aux not found", status_code=404)
        await db.execute(
            """UPDATE auxes SET kind=?, model=?, capabilities_json=?,
               interface=?, observability=?, pool=?, status=? WHERE id=?""",
            (
                kind,
                model,
                json.dumps(_parse_caps(capabilities)),
                interface,
                observability,
                pool,
                status,
                aux_id,
            ),
        )
        await db.commit()
    return _redirect("/ui/hardware")


@router.delete("/hardware/{aux_id}", response_class=HTMLResponse, include_in_schema=False)
async def delete_hardware(
    request: Request, aux_id: str, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        await db.execute("DELETE FROM connections WHERE aux_id = ?", (aux_id,))
        await db.execute("DELETE FROM auxes WHERE id = ?", (aux_id,))
        await db.commit()
    return HTMLResponse("")


# ---------------------------------------------------------------------------
# Peripherals CRUD
# ---------------------------------------------------------------------------


@router.get("/peripherals/form", response_class=HTMLResponse, include_in_schema=False)
async def new_peripheral_form(
    request: Request, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    devices = await _devices(db_path)
    return _tr(
        request,
        "peripherals_form.html",
        {"peripheral": None, "devices": devices, "selected_device_ids": []},
    )


@router.get("/peripherals/{periph_id}/form", response_class=HTMLResponse, include_in_schema=False)
async def edit_peripheral_form(
    request: Request, periph_id: str, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        async with db.execute("SELECT * FROM peripherals WHERE id = ?", (periph_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return HTMLResponse("Peripheral not found", status_code=404)
        async with db.execute(
            "SELECT device_id FROM device_peripherals WHERE peripheral_id = ?", (periph_id,)
        ) as cur:
            selected = [r["device_id"] for r in await cur.fetchall()]
    p = dict(row)
    p["specs"] = _parse_specs(p.get("specs_json"))
    devices = await _devices(db_path)
    return _tr(
        request,
        "peripherals_form.html",
        {"peripheral": p, "devices": devices, "selected_device_ids": selected},
    )


def _build_specs_json(resolution: str, controller: str, interface: str) -> str | None:
    """Pack the structured display fields into a specs_json blob (None if all empty)."""
    specs = {}
    if resolution.strip():
        specs["resolution"] = resolution.strip()
    if controller.strip():
        specs["controller"] = controller.strip()
    if interface.strip():
        specs["interface"] = interface.strip()
    return json.dumps(specs) if specs else None


async def _sync_device_peripherals(db, periph_id: str, device_ids: list[str]) -> None:
    """Replace this peripheral's device associations with the selected set."""
    await db.execute("DELETE FROM device_peripherals WHERE peripheral_id = ?", (periph_id,))
    for did in device_ids:
        if did:
            await db.execute(
                "INSERT OR IGNORE INTO device_peripherals (device_id, peripheral_id) VALUES (?, ?)",
                (did, periph_id),
            )


@router.post("/peripherals", response_class=HTMLResponse, include_in_schema=False)
async def create_peripheral(
    request: Request,
    hil_token: str = Cookie(default=""),
    id: Annotated[str, Form()] = "",
    kind: Annotated[str, Form()] = "display",
    model: Annotated[str, Form()] = "",
    product_url: Annotated[str, Form()] = "",
    resolution: Annotated[str, Form()] = "",
    controller: Annotated[str, Form()] = "",
    interface: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
    device_ids: Annotated[list[str], Form()] = [],
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()

    async def _err(msg: str) -> HTMLResponse:
        return _tr(
            request,
            "peripherals_form.html",
            {
                "peripheral": None,
                "devices": await _devices(request.app.state.db_path),
                "selected_device_ids": device_ids,
                "error": msg,
            },
        )

    if not id:
        return await _err("ID is required")
    db_path: str = request.app.state.db_path
    specs_json = _build_specs_json(resolution, controller, interface)
    async with get_db(db_path) as db:
        try:
            await db.execute(
                "INSERT INTO peripherals (id, kind, model, product_url, specs_json, notes) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (id, kind, model, product_url or None, specs_json, notes or None),
            )
            await _sync_device_peripherals(db, id, device_ids)
            await db.commit()
        except Exception as exc:
            return await _err(str(exc))
    return _redirect("/ui/hardware")


@router.post("/peripherals/{periph_id}", response_class=HTMLResponse, include_in_schema=False)
async def update_peripheral(
    request: Request,
    periph_id: str,
    hil_token: str = Cookie(default=""),
    kind: Annotated[str, Form()] = "display",
    model: Annotated[str, Form()] = "",
    product_url: Annotated[str, Form()] = "",
    resolution: Annotated[str, Form()] = "",
    controller: Annotated[str, Form()] = "",
    interface: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
    device_ids: Annotated[list[str], Form()] = [],
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    specs_json = _build_specs_json(resolution, controller, interface)
    async with get_db(db_path) as db:
        async with db.execute("SELECT id FROM peripherals WHERE id = ?", (periph_id,)) as cur:
            if await cur.fetchone() is None:
                return HTMLResponse("Peripheral not found", status_code=404)
        await db.execute(
            "UPDATE peripherals SET kind=?, model=?, product_url=?, specs_json=?, notes=? WHERE id=?",  # noqa: E501
            (kind, model, product_url or None, specs_json, notes or None, periph_id),
        )
        await _sync_device_peripherals(db, periph_id, device_ids)
        await db.commit()
    return _redirect("/ui/hardware")


@router.delete("/peripherals/{periph_id}", response_class=HTMLResponse, include_in_schema=False)
async def delete_peripheral(
    request: Request, periph_id: str, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        await db.execute("DELETE FROM device_peripherals WHERE peripheral_id = ?", (periph_id,))
        await db.execute("DELETE FROM peripherals WHERE id = ?", (periph_id,))
        await db.commit()
    return HTMLResponse("")


# ---------------------------------------------------------------------------
# I2C strand CRUD (strands / strand_components / device_strands)
# ---------------------------------------------------------------------------


async def _strands_web_list(db_path: str) -> list[dict]:
    from hil_controller.api.strands import _load_strand

    async with get_db(db_path) as db:
        async with db.execute("SELECT id FROM strands ORDER BY id") as cur:
            ids = [r["id"] for r in await cur.fetchall()]
        return [await _load_strand(db, sid) for sid in ids]


def _parse_addr(value: str) -> int | None:
    value = (value or "").strip()
    return int(value, 0) if value else None


@router.get("/strands/form", response_class=HTMLResponse, include_in_schema=False)
async def new_strand_form(request: Request, hil_token: str = Cookie(default="")) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    return _tr(request, "strands_form.html", {"strand": None})


@router.get("/strands/{strand_id}/form", response_class=HTMLResponse, include_in_schema=False)
async def edit_strand_form(
    request: Request, strand_id: str, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    from hil_controller.api.strands import _load_strand

    async with get_db(request.app.state.db_path) as db:
        s = await _load_strand(db, strand_id)
    if s is None:
        return HTMLResponse("Strand not found", status_code=404)
    s["components_json"] = json.dumps(s.get("components") or [], indent=2)
    s["routes_json"] = json.dumps(s.get("routes") or [], indent=2)
    return _tr(request, "strands_form.html", {"strand": s})


async def _save_strand_web(request, strand_id, form, is_create):
    from hil_controller.api.strands import Strand, _load_strand, _write_strand

    def _render_err(msg: str) -> HTMLResponse:
        ctx = dict(form)
        ctx["error"] = msg
        return _tr(request, "strands_form.html", {"strand": ctx})

    sid = form["id"] if is_create else strand_id
    if not sid:
        return _render_err("ID is required")
    try:
        strand = Strand(
            id=sid,
            mux_aux=form.get("mux_aux") or None,
            mux_group=form.get("mux_group") or None,
            tca_address=_parse_addr(form.get("tca_address", "")),
            pool=form.get("pool") or "public",
            status=form.get("status") or "available",
            notes=form.get("notes") or None,
            components=json.loads(form.get("components_json") or "[]"),
            routes=json.loads(form.get("routes_json") or "[]"),
        )
    except Exception as exc:  # noqa: BLE001 - surface JSON/validation errors in the form
        return _render_err(f"invalid components/routes JSON or fields: {exc}")
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        if is_create and await _load_strand(db, sid) is not None:
            return _render_err(f"strand {sid!r} already exists")
        try:
            await _write_strand(db, strand)
        except Exception as exc:  # noqa: BLE001 - e.g. a route to an unknown device (FK)
            return _render_err(str(exc))
    return _redirect("/ui/hardware")


@router.post("/strands", response_class=HTMLResponse, include_in_schema=False)
async def create_strand_web(
    request: Request,
    hil_token: str = Cookie(default=""),
    id: Annotated[str, Form()] = "",
    mux_aux: Annotated[str, Form()] = "",
    mux_group: Annotated[str, Form()] = "",
    tca_address: Annotated[str, Form()] = "",
    pool: Annotated[str, Form()] = "public",
    status: Annotated[str, Form()] = "available",
    notes: Annotated[str, Form()] = "",
    components_json: Annotated[str, Form()] = "[]",
    routes_json: Annotated[str, Form()] = "[]",
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    return await _save_strand_web(request, id, locals(), is_create=True)


@router.post("/strands/{strand_id}", response_class=HTMLResponse, include_in_schema=False)
async def update_strand_web(
    request: Request,
    strand_id: str,
    hil_token: str = Cookie(default=""),
    mux_aux: Annotated[str, Form()] = "",
    mux_group: Annotated[str, Form()] = "",
    tca_address: Annotated[str, Form()] = "",
    pool: Annotated[str, Form()] = "public",
    status: Annotated[str, Form()] = "available",
    notes: Annotated[str, Form()] = "",
    components_json: Annotated[str, Form()] = "[]",
    routes_json: Annotated[str, Form()] = "[]",
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    return await _save_strand_web(request, strand_id, locals(), is_create=False)


@router.delete("/strands/{strand_id}", response_class=HTMLResponse, include_in_schema=False)
async def delete_strand_web(
    request: Request, strand_id: str, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    async with get_db(request.app.state.db_path) as db:
        await db.execute("DELETE FROM strands WHERE id = ?", (strand_id,))
        await db.commit()
    return HTMLResponse("")


# ---------------------------------------------------------------------------
# Cameras CRUD (cameras table)
# ---------------------------------------------------------------------------


@router.get("/cameras", response_class=HTMLResponse, include_in_schema=False)
async def cameras_page(request: Request, hil_token: str = Cookie(default="")) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    cameras = await _cameras(db_path)
    return _tr(
        request, "cameras.html", {"token": hil_token, "active": "cameras", "cameras": cameras}
    )


@router.get("/cameras/form", response_class=HTMLResponse, include_in_schema=False)
async def new_camera_form(request: Request, hil_token: str = Cookie(default="")) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    return _tr(request, "cameras_form.html", {"camera": None})


@router.get("/cameras/preview", include_in_schema=False)
async def camera_url_preview(
    request: Request, url: str = "", hil_token: str = Cookie(default="")
) -> Response:
    """Proxy a user-supplied camera URL so the form preview can load it without CORS issues."""
    if not (await _check_web_token(request, hil_token)):
        return Response(status_code=401)
    if not url:
        return Response(status_code=400)
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            return Response(content=r.content, media_type="image/jpeg")
    except Exception:
        return Response(status_code=503)


@router.get("/cameras/{cam_id}/form", response_class=HTMLResponse, include_in_schema=False)
async def edit_camera_form(
    request: Request, cam_id: str, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    cam = await _camera_by_id(db_path, cam_id)
    if cam is None:
        return HTMLResponse("Camera not found", status_code=404)
    return _tr(request, "cameras_form.html", {"camera": cam})


@router.post("/cameras", response_class=HTMLResponse, include_in_schema=False)
async def create_camera(
    request: Request,
    hil_token: str = Cookie(default=""),
    id: Annotated[str, Form()] = "",
    model: Annotated[str, Form()] = "",
    host_id: Annotated[str, Form()] = "",
    kind: Annotated[str, Form()] = "",
    stream_url: Annotated[list[str], Form()] = [],
    stream_type: Annotated[list[str], Form()] = [],
    pool: Annotated[str, Form()] = "public",
    status: Annotated[str, Form()] = "available",
    notes: Annotated[str, Form()] = "",
) -> Response:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    streams = [{"url": u.strip(), "type": t} for u, t in zip(stream_url, stream_type) if u.strip()]
    if not id or not streams:
        return _tr(
            request,
            "cameras_form.html",
            {"camera": None, "error": "ID and at least one stream URL are required"},
        )
    primary_url = streams[0]["url"]
    streams_json = json.dumps(streams)
    async with get_db(db_path) as db:
        try:
            await db.execute(
                """INSERT INTO cameras
                   (id, host_id, source, kind, model, pool, status, notes, streams_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    id,
                    host_id or None,
                    primary_url,
                    kind or None,
                    model,
                    pool,
                    status,
                    notes or None,
                    streams_json,
                ),
            )
            await db.commit()
        except Exception as exc:
            return _tr(request, "cameras_form.html", {"camera": None, "error": str(exc)})
    return _redirect("/ui/cameras")


@router.post("/cameras/{cam_id}", response_class=HTMLResponse, include_in_schema=False)
async def update_camera(
    request: Request,
    cam_id: str,
    hil_token: str = Cookie(default=""),
    model: Annotated[str, Form()] = "",
    host_id: Annotated[str, Form()] = "",
    kind: Annotated[str, Form()] = "",
    stream_url: Annotated[list[str], Form()] = [],
    stream_type: Annotated[list[str], Form()] = [],
    pool: Annotated[str, Form()] = "public",
    status: Annotated[str, Form()] = "available",
    notes: Annotated[str, Form()] = "",
) -> Response:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    streams = [{"url": u.strip(), "type": t} for u, t in zip(stream_url, stream_type) if u.strip()]
    primary_url = streams[0]["url"] if streams else ""
    streams_json = json.dumps(streams) if streams else None
    async with get_db(db_path) as db:
        async with db.execute("SELECT id FROM cameras WHERE id = ?", (cam_id,)) as cur:
            if await cur.fetchone() is None:
                return HTMLResponse("Camera not found", status_code=404)
        await db.execute(
            """UPDATE cameras SET model=?, host_id=?, kind=?, source=?, pool=?, status=?,
               notes=?, streams_json=? WHERE id=?""",
            (
                model,
                host_id or None,
                kind or None,
                primary_url,
                pool,
                status,
                notes or None,
                streams_json,
                cam_id,
            ),
        )
        await db.commit()
    return _redirect("/ui/cameras")


@router.delete("/cameras/{cam_id}", response_class=HTMLResponse, include_in_schema=False)
async def delete_camera(
    request: Request, cam_id: str, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        await db.execute("DELETE FROM camera_rois WHERE camera_id = ?", (cam_id,))
        await db.execute("DELETE FROM cameras WHERE id = ?", (cam_id,))
        await db.commit()
    return HTMLResponse("")


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------


@router.post("/connections", response_class=HTMLResponse, include_in_schema=False)
async def create_connection(
    request: Request,
    hil_token: str = Cookie(default=""),
    aux_id: Annotated[str, Form()] = "",
    device_id: Annotated[str, Form()] = "",
    mux_id: Annotated[str, Form()] = "",
    mux_channel: Annotated[str, Form()] = "",
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    if not aux_id or not device_id:
        return HTMLResponse("aux_id and device_id are required", status_code=422)
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        await db.execute(
            "INSERT INTO connections (aux_id, device_id, mux_id, mux_channel) VALUES (?, ?, ?, ?)",
            (aux_id, device_id, mux_id or None, mux_channel or None),
        )
        await db.commit()
        async with db.execute("SELECT * FROM connections WHERE aux_id = ?", (aux_id,)) as cur:
            conns = [dict(c) for c in await cur.fetchall()]
    return _tr(request, "conn_list.html", {"connections": conns, "aux_id": aux_id})


@router.delete("/connections/{conn_id}", response_class=HTMLResponse, include_in_schema=False)
async def delete_connection(
    request: Request, conn_id: int, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        await db.execute("DELETE FROM connections WHERE id = ?", (conn_id,))
        await db.commit()
    return HTMLResponse("")


# ---------------------------------------------------------------------------
# Scripts browser
# ---------------------------------------------------------------------------


@router.get("/scripts", response_class=HTMLResponse, include_in_schema=False)
async def scripts_page(request: Request, hil_token: str = Cookie(default="")) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    from hil_controller.config import get_settings

    scripts_dir = get_settings().scripts_dir
    scripts = []
    if scripts_dir:
        p = Path(scripts_dir)
        for jf in sorted(p.glob("*.json")):
            try:
                data = json.loads(jf.read_text())
                scripts.append(
                    {
                        "filename": jf.name,
                        "name": data.get("name", jf.stem),
                        "description": data.get("description", ""),
                        "proto_version": data.get("protoVersion", ""),
                        "step_count": len(data.get("steps", [])),
                    }
                )
            except Exception:
                scripts.append(
                    {
                        "filename": jf.name,
                        "name": jf.stem,
                        "description": "",
                        "proto_version": "",
                        "step_count": 0,
                    }
                )
    return _tr(
        request,
        "scripts.html",
        {"token": hil_token, "active": "scripts", "scripts": scripts, "scripts_dir": scripts_dir},
    )


@router.get("/scripts/{filename}", response_class=HTMLResponse, include_in_schema=False)
async def script_detail(
    request: Request, filename: str, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    from hil_controller.config import get_settings

    scripts_dir = get_settings().scripts_dir
    if not scripts_dir:
        return HTMLResponse("Scripts directory not configured", status_code=404)
    safe_name = Path(filename).name
    fpath = Path(scripts_dir) / safe_name
    if not fpath.exists() or fpath.suffix != ".json":
        return HTMLResponse("Script not found", status_code=404)
    try:
        data = json.loads(fpath.read_text())
    except Exception as exc:
        return HTMLResponse(f"Parse error: {exc}", status_code=500)
    name = data.get("name", safe_name)
    desc = data.get("description", "")
    body = json.dumps(data, indent=2)
    return HTMLResponse(
        f'<div class="script-card" style="cursor:default;">'
        f"<h3>{name}</h3>"
        f"<p>{desc}</p></div>"
        f"<pre><code>{body}</code></pre>"
    )


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def _duration(started: str | None, finished: str | None) -> str:
    if not started or not finished:
        return ""
    try:
        s = datetime.fromisoformat(started)
        f = datetime.fromisoformat(finished)
        secs = int((f - s).total_seconds())
        return f"{secs // 60}m {secs % 60}s"
    except Exception:
        return ""


async def _job_rows(db_path: str, limit: int = 100) -> list[dict]:
    async with get_db(db_path) as db:
        async with db.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
    result = []
    for r in rows:
        j = dict(r)
        req = json.loads(j.get("request_json") or "{}")
        src = (req.get("payload") or {}).get("source") or {}
        j["repo_url"] = src.get("repo", "")
        j["ref"] = src.get("ref", "")
        j["duration"] = _duration(j.get("started_at"), j.get("finished_at"))
        result.append(j)
    return result


def _render_events(events: list[dict]) -> str:
    lines = []
    colours = {
        "stdout": "#c9d1d9",
        "stderr": "#f97583",
        "protomq": "#79c0ff",
        "serial": "#7ee787",  # SerialCaptureAdapter on_line events
        "state": "#d2a8ff",
    }
    for ev in events:
        kind = ev.get("kind", "")
        payload = ev.get("payload", {})
        at = ev.get("at", "")[:19]
        if kind == "log":
            stream = payload.get("stream", "stdout")
            msg = html.escape(payload.get("msg", ""))
            colour = colours.get(stream, "#c9d1d9")
            lines.append(
                f'<div style="color:{colour};font-family:monospace;font-size:0.75rem;white-space:pre-wrap;">'  # noqa: E501
                f'<span style="color:#6c757d;user-select:none;">[{at}] </span>{msg}</div>'
            )
        elif kind == "state":
            st = payload.get("state", "")
            colour = colours["state"]
            lines.append(
                f'<div style="color:{colour};font-family:monospace;font-size:0.75rem;">'
                f'<span style="color:#6c757d;user-select:none;">[{at}] </span>'
                f"<b>── state: {html.escape(st)} ──</b></div>"
            )
    return (
        "\n".join(lines)
        if lines
        else '<span style="color:#6c757d;font-size:0.8rem;">No output yet.</span>'
    )


_JOB_DEFAULTS = {
    "no_hw_cmd": '.venv/bin/python -m pytest -m "not hardware" -v --tb=short',
    "hw_cmd": '.venv/bin/python -m pytest -m "display or hardware" -v --tb=short',
}


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} PB"


def _disk_info(path: str = "") -> dict:
    try:
        p = path or "/"
        u = shutil.disk_usage(p)
        pct = int(100 * u.used / u.total) if u.total else 0
        return {
            "total_fmt": _fmt_bytes(u.total),
            "free_fmt": _fmt_bytes(u.free),
            "used_fmt": _fmt_bytes(u.used),
            "pct_used": pct,
            "free": u.free,
        }
    except Exception:
        return {"total_fmt": "?", "free_fmt": "?", "used_fmt": "?", "pct_used": 0, "free": 0}


def _jobs_dir() -> str:
    from hil_controller.config import resolve_jobs_dir

    return resolve_jobs_dir()


async def _asset_rows(db_path: str) -> list[dict]:
    async with get_db(db_path) as db:
        async with db.execute("SELECT * FROM assets ORDER BY created_at DESC") as cur:
            rows = await cur.fetchall()
    result = []
    for r in rows:
        a = dict(r)
        a["size_fmt"] = _fmt_bytes(a.get("size_bytes") or 0)
        result.append(a)
    return result


async def _call_jobs_api(request: Request, job_request: dict, token: str) -> dict:
    """Submit a job via the internal /v1/jobs API and return the response dict."""
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=request.app), base_url="http://test") as c:
        r = await c.post(
            "/v1/jobs",
            json=job_request,
            headers={"Authorization": f"Bearer {token}"},
        )
    if r.status_code not in (200, 202):
        raise ValueError(r.json().get("detail", f"HTTP {r.status_code}"))
    return r.json()


async def _call_jobs_api_path(request: Request, path: str, body: dict, token: str) -> dict:
    """POST an arbitrary internal /v1 path (e.g. a job action) and return JSON."""
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=request.app), base_url="http://test") as c:
        r = await c.post(path, json=body, headers={"Authorization": f"Bearer {token}"})
    if r.status_code not in (200, 202):
        raise ValueError(r.json().get("detail", f"HTTP {r.status_code}"))
    return r.json()


def _build_job_request(
    *,
    repo: str,
    ref: str,
    pat: str,
    submodules: bool,
    setup: str,
    hw_mode: str,
    test_cmd: str,
    protomq_script: str,
    device_id: str,
    requires_aux: str,
    secrets_profile: str,
    mqtt_host: str,
    mqtt_port: str,
    io_username: str,
    io_key: str,
    timeout_total: int,
    timeout_run: int,
    timeout_deploy: int,
) -> dict:
    extra_env: dict = {}
    if hw_mode == "no_hardware":
        extra_env["BLINKA_OS_AGNOSTIC"] = "1"

    target: dict = {"pool": "wippersnapper-python"}
    if device_id:
        target["device"] = {"id": device_id}
    else:
        target["device"] = {"kind": "sbc", "capabilities": ["python-snapper"]}
    if requires_aux:
        target["requires"] = [{"id": requires_aux}]

    _mqtt_port = int(mqtt_port) if mqtt_port.strip().isdigit() else 1884
    params: dict = {
        "entry": "bash",
        "args": ["-c", test_cmd.replace("\r\n", "\n").replace("\r", "\n")],
        "secrets_format": "dotenv",
        "extra_env": extra_env,
    }
    if protomq_script and mqtt_host:
        params["protomq"] = {
            "broker_host": mqtt_host,
            "mqtt_port": _mqtt_port,
            "api_port": 5173,
            "script": protomq_script,
        }

    source: dict = {
        "repo": repo,
        "ref": ref,
        "shallow": True,
        "submodules": submodules,
        "setup": ["bash", "-c", setup.replace("\r\n", "\n").replace("\r", "\n")]
        if setup.strip()
        else [],
    }
    if pat:
        source["pat"] = pat

    secrets: dict = {"MQTT_HOST": mqtt_host, "MQTT_PORT": str(_mqtt_port)}
    if io_username:
        secrets["IO_USERNAME"] = io_username
    if io_key:
        secrets["IO_KEY"] = io_key

    return {
        "target": target,
        "script": "pytest-suite",
        "payload": {"kind": "git-source", "source": source},
        "params": params,
        "secrets": secrets,
        "secrets_profile": secrets_profile,
        "timeouts": {
            "total_s": timeout_total,
            "deploy_s": timeout_deploy,
            "run_s": timeout_run,
            "flash_s": 120,
        },
    }


@router.get("/jobs", response_class=HTMLResponse, include_in_schema=False)
async def jobs_page(request: Request, hil_token: str = Cookie(default="")) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    jobs = await _job_rows(db_path)
    return _tr(
        request,
        "jobs.html",
        {"token": hil_token, "active": "jobs", "jobs": jobs, "disk": _disk_info(_jobs_dir())},
    )


@router.get("/jobs/list", response_class=HTMLResponse, include_in_schema=False)
async def jobs_list_partial(request: Request, hil_token: str = Cookie(default="")) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return HTMLResponse("", status_code=401)
    db_path: str = request.app.state.db_path
    jobs = await _job_rows(db_path)
    return _tr(request, "jobs_body.html", {"jobs": jobs})


@router.get("/jobs/new", response_class=HTMLResponse, include_in_schema=False)
async def new_job_page(request: Request, hil_token: str = Cookie(default="")) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    sbc_devices = [d for d in await _devices(db_path) if d["kind"] == "sbc"]

    from hil_controller.config import get_settings

    scripts_dir = get_settings().scripts_dir
    scripts = (
        sorted(Path(scripts_dir).glob("*.json"))
        if scripts_dir and Path(scripts_dir).exists()
        else []
    )

    return _tr(
        request,
        "job_new.html",
        {
            "token": hil_token,
            "active": "jobs",
            "sbc_devices": sbc_devices,
            "scripts": scripts,
            "defaults": _JOB_DEFAULTS,
            "disk": _disk_info(_jobs_dir()),
            "form": None,
            "error": None,
        },
    )


@router.get("/jobs/new-arduino-ws", response_class=HTMLResponse, include_in_schema=False)
async def new_arduino_ws_job_page(
    request: Request, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    mcu_devices = [d for d in await _devices(db_path) if d["kind"] == "microcontroller"]
    from hil_controller.config import get_settings

    cfg = get_settings()
    scripts_dir = cfg.scripts_dir
    scripts = (
        sorted(Path(scripts_dir).glob("*.json"))
        if scripts_dir and Path(scripts_dir).exists()
        else []
    )
    return _tr(
        request,
        "job_new_arduino_ws.html",
        {
            "token": hil_token,
            "active": "jobs",
            "mcu_devices": mcu_devices,
            "scripts": scripts,
            "cfg": {
                "wippersnapper_repo": cfg.wippersnapper_arduino_repo,
                "protomq_repo": cfg.protomq_repo,
                "protomq_default_ref": cfg.protomq_default_ref,
                "pio_default_env": cfg.pio_default_env,
                "serial_default_port": cfg.serial_default_port,
                "mqtt_default_host": cfg.mqtt_default_host,
            },
            "defaults": _ARDUINO_WS_DEFAULTS,
            "default_build_steps": _default_build_steps(
                cfg.pio_default_env, cfg.serial_default_port
            ),
            "disk": _disk_info(_jobs_dir()),
            "form": None,
            "error": None,
        },
    )


def _parse_github_repo(url: str) -> tuple[str, str] | None:
    """Return (owner, repo) for a github.com URL, else None."""
    import re

    m = re.match(
        r"^(?:https?://)?(?:[\w.-]+@)?(?:www\.)?github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?/?$",
        url.strip(),
    )
    if not m:
        return None
    return m.group(1), m.group(2)


@router.get("/jobs/arduino-ws/scripts", response_class=HTMLResponse, include_in_schema=False)
async def arduino_ws_scripts_refresh(
    request: Request,
    hil_token: str = Cookie(default=""),
    protomq_repo: str = "",
    protomq_ref: str = "",
    pat: str = "",
) -> HTMLResponse:
    """Return <option> tags for protoMQ scripts/ at the given repo+ref.

    Uses the GitHub contents API (no clone). HTMX swaps these into the
    #protomq_script <select>.
    """
    if not (await _check_web_token(request, hil_token)):
        return HTMLResponse("", status_code=401)

    parsed = _parse_github_repo(protomq_repo)
    if parsed is None:
        return HTMLResponse(
            '<option value="">(only github.com repos supported for refresh)</option>'
        )
    owner, repo = parsed
    ref = protomq_ref.strip() or _ARDUINO_WS_DEFAULTS["protomq_ref"]
    api = f"https://api.github.com/repos/{owner}/{repo}/contents/scripts"

    headers = {"Accept": "application/vnd.github+json"}
    if pat.strip():
        headers["Authorization"] = f"Bearer {pat.strip()}"
    from httpx import AsyncClient

    try:
        async with AsyncClient(timeout=10.0) as c:
            r = await c.get(api, params={"ref": ref}, headers=headers)
        if r.status_code != 200:
            return HTMLResponse(
                f'<option value="">(github API {r.status_code} for {html.escape(owner)}/'
                f"{html.escape(repo)}@{html.escape(ref)})</option>"
            )
        entries = r.json()
    except Exception as exc:
        return HTMLResponse(f'<option value="">(refresh failed: {html.escape(str(exc))})</option>')

    stems = sorted(
        e["name"][:-5]
        for e in entries
        if isinstance(e, dict) and e.get("type") == "file" and e.get("name", "").endswith(".json")
    )
    opts = ['<option value="">None / not needed</option>']
    opts += [f'<option value="{html.escape(s)}">{html.escape(s)}</option>' for s in stems]
    return HTMLResponse("\n".join(opts))


@router.post("/jobs/arduino-ws", include_in_schema=False, response_model=None)
async def submit_arduino_ws_job(
    request: Request,
    hil_token: str = Cookie(default=""),
    wippersnapper_repo: Annotated[str, Form()] = "",
    wippersnapper_ref: Annotated[str, Form()] = "",
    protomq_repo: Annotated[str, Form()] = "",
    protomq_ref: Annotated[str, Form()] = "",
    pat: Annotated[str, Form()] = "",
    submodules: Annotated[str, Form()] = "",
    pio_env: Annotated[str, Form()] = "",
    serial_port: Annotated[str, Form()] = "",
    build_host: Annotated[str, Form()] = "controller",
    flash_mode: Annotated[str, Form()] = "usbip",
    test_host: Annotated[str, Form()] = "none",
    protomq_host: Annotated[str, Form()] = "controller",
    build_steps: Annotated[str, Form()] = "",
    setup: Annotated[str, Form()] = "",
    test_cmd: Annotated[
        str, Form()
    ] = ". .venv/bin/activate && python -m pytest tests/ -v --tb=short",
    protomq_script: Annotated[str, Form()] = "",
    device_id: Annotated[str, Form()] = "",
    secrets_profile: Annotated[str, Form()] = "bench-protomq",
    mqtt_host: Annotated[str, Form()] = "",
    mqtt_port: Annotated[str, Form()] = "1884",
    io_username: Annotated[str, Form()] = "",
    io_key: Annotated[str, Form()] = "",
    timeout_total: Annotated[str, Form()] = "1200",
    timeout_run: Annotated[str, Form()] = "300",
    timeout_deploy: Annotated[str, Form()] = "900",
) -> Response:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()

    from hil_controller.config import get_settings

    cfg = get_settings()
    if not wippersnapper_repo:
        wippersnapper_repo = cfg.wippersnapper_arduino_repo
    if not protomq_repo:
        protomq_repo = cfg.protomq_repo
    if not protomq_ref:
        protomq_ref = _ARDUINO_WS_DEFAULTS["protomq_ref"]
    if not wippersnapper_ref:
        wippersnapper_ref = _ARDUINO_WS_DEFAULTS["wippersnapper_ref"]
    if not pio_env:
        pio_env = cfg.pio_default_env
    if not serial_port:
        serial_port = cfg.serial_default_port
    if not mqtt_host:
        mqtt_host = cfg.mqtt_default_host

    db_path: str = request.app.state.db_path
    mcu_devices = [d for d in await _devices(db_path) if d["kind"] == "microcontroller"]
    scripts_dir = cfg.scripts_dir
    scripts = (
        sorted(Path(scripts_dir).glob("*.json"))
        if scripts_dir and Path(scripts_dir).exists()
        else []
    )

    form_vals = {
        "wippersnapper_repo": wippersnapper_repo,
        "wippersnapper_ref": wippersnapper_ref,
        "protomq_repo": protomq_repo,
        "protomq_ref": protomq_ref,
        "pat": pat,
        "submodules": bool(submodules),
        "pio_env": pio_env,
        "serial_port": serial_port,
        "build_host": build_host,
        "flash_mode": flash_mode,
        "test_host": test_host,
        "protomq_host": protomq_host,
        "build_steps": build_steps,
        "setup": setup,
        "test_cmd": test_cmd,
        "protomq_script": protomq_script,
        "device_id": device_id,
        "secrets_profile": secrets_profile,
        "mqtt_host": mqtt_host,
        "mqtt_port": mqtt_port,
        "io_username": io_username,
        "io_key": io_key,
        "timeout_total": timeout_total,
        "timeout_run": timeout_run,
        "timeout_deploy": timeout_deploy,
    }

    def _ctx(error: str) -> dict:
        return {
            "token": hil_token,
            "active": "jobs",
            "mcu_devices": mcu_devices,
            "scripts": scripts,
            "cfg": {
                "wippersnapper_repo": cfg.wippersnapper_arduino_repo,
                "protomq_repo": cfg.protomq_repo,
                "protomq_default_ref": cfg.protomq_default_ref,
                "pio_default_env": cfg.pio_default_env,
                "serial_default_port": cfg.serial_default_port,
                "mqtt_default_host": cfg.mqtt_default_host,
            },
            "defaults": _ARDUINO_WS_DEFAULTS,
            "default_build_steps": _default_build_steps(pio_env, serial_port),
            "disk": _disk_info(_jobs_dir()),
            "form": form_vals,
            "error": error,
        }

    try:
        job_req = _build_arduino_ws_job_request(
            wippersnapper_repo=wippersnapper_repo,
            wippersnapper_ref=wippersnapper_ref,
            protomq_repo=protomq_repo,
            protomq_ref=protomq_ref,
            pat=pat,
            submodules=bool(submodules),
            pio_env=pio_env,
            serial_port=serial_port,
            build_steps=build_steps,
            setup=setup,
            test_cmd=test_cmd,
            protomq_script=protomq_script,
            device_id=device_id,
            secrets_profile=secrets_profile,
            mqtt_host=mqtt_host,
            mqtt_port=mqtt_port,
            io_username=io_username,
            io_key=io_key,
            timeout_total=int(timeout_total or 1200),
            timeout_run=int(timeout_run or 300),
            timeout_deploy=int(timeout_deploy or 900),
            build_host=build_host,
            flash_mode=flash_mode,
            test_host=test_host,
            protomq_host=protomq_host,
            controller_ip=cfg.controller_ip,
        )
        resp = await _call_jobs_api(request, job_req, hil_token)
        job_id = resp["id"]
    except Exception as exc:
        return _tr(request, "job_new_arduino_ws.html", _ctx(str(exc)))

    return RedirectResponse(f"/ui/jobs/{job_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/jobs", include_in_schema=False, response_model=None)
async def submit_job_form(
    request: Request,
    hil_token: str = Cookie(default=""),
    repo: Annotated[str, Form()] = "",
    ref: Annotated[str, Form()] = "main",
    pat: Annotated[str, Form()] = "",
    submodules: Annotated[str, Form()] = "",
    setup: Annotated[
        str, Form()
    ] = "sudo apt install -y python3-venv &&\npython3 -m venv .venv &&\n. ./.venv/bin/activate &&\npip install -e .",  # noqa: E501
    hw_mode: Annotated[str, Form()] = "no_hardware",
    test_cmd: Annotated[str, Form()] = '.venv/bin/python -m pytest -m "not hardware" -v --tb=short',
    protomq_script: Annotated[str, Form()] = "",
    device_id: Annotated[str, Form()] = "",
    requires_aux: Annotated[str, Form()] = "",
    secrets_profile: Annotated[str, Form()] = "bench-protomq",
    mqtt_host: Annotated[str, Form()] = "127.0.0.1",
    mqtt_port: Annotated[str, Form()] = "1884",
    io_username: Annotated[str, Form()] = "",
    io_key: Annotated[str, Form()] = "",
    timeout_total: Annotated[str, Form()] = "600",
    timeout_run: Annotated[str, Form()] = "300",
    timeout_deploy: Annotated[str, Form()] = "180",
) -> Response:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()

    db_path: str = request.app.state.db_path
    sbc_devices = [d for d in await _devices(db_path) if d["kind"] == "sbc"]
    from hil_controller.config import get_settings

    scripts_dir = get_settings().scripts_dir
    scripts = (
        sorted(Path(scripts_dir).glob("*.json"))
        if scripts_dir and Path(scripts_dir).exists()
        else []
    )

    form_vals = {
        "repo": repo,
        "ref": ref,
        "pat": pat,
        "setup": setup,
        "submodules": bool(submodules),
        "hw_mode": hw_mode,
        "test_cmd": test_cmd,
        "protomq_script": protomq_script,
        "device_id": device_id,
        "requires_aux": requires_aux,
        "secrets_profile": secrets_profile,
        "mqtt_host": mqtt_host,
        "mqtt_port": mqtt_port,
        "io_username": io_username,
        "io_key": io_key,
        "timeout_total": timeout_total,
        "timeout_run": timeout_run,
        "timeout_deploy": timeout_deploy,
    }

    if not repo:
        return _tr(
            request,
            "job_new.html",
            {
                "token": hil_token,
                "active": "jobs",
                "sbc_devices": sbc_devices,
                "scripts": scripts,
                "defaults": _JOB_DEFAULTS,
                "disk": _disk_info(_jobs_dir()),
                "form": form_vals,
                "error": "Repository URL is required",
            },
        )

    try:
        job_req = _build_job_request(
            repo=repo,
            ref=ref,
            pat=pat,
            submodules=bool(submodules),
            setup=setup,
            hw_mode=hw_mode,
            test_cmd=test_cmd,
            protomq_script=protomq_script,
            device_id=device_id,
            requires_aux=requires_aux,
            secrets_profile=secrets_profile,
            mqtt_host=mqtt_host,
            mqtt_port=mqtt_port,
            io_username=io_username,
            io_key=io_key,
            timeout_total=int(timeout_total or 600),
            timeout_run=int(timeout_run or 300),
            timeout_deploy=int(timeout_deploy or 180),
        )
        resp = await _call_jobs_api(request, job_req, hil_token)
        job_id = resp["id"]
    except Exception as exc:
        return _tr(
            request,
            "job_new.html",
            {
                "token": hil_token,
                "active": "jobs",
                "sbc_devices": sbc_devices,
                "scripts": scripts,
                "defaults": _JOB_DEFAULTS,
                "disk": _disk_info(_jobs_dir()),
                "form": form_vals,
                "error": str(exc),
            },
        )

    return RedirectResponse(f"/ui/jobs/{job_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/jobs/new-firmware-bench", response_class=HTMLResponse, include_in_schema=False)
async def new_firmware_bench_page(
    request: Request, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    # Defined before the /jobs/{job_id} catch-all so the literal path isn't
    # shadowed (the same reason new-arduino-ws lives up here).
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    mcu_devices = [d for d in await _devices(db_path) if d["kind"] == "microcontroller"]
    assets = await _asset_rows(db_path)
    recent_assets = [a for a in assets if a["kind"] == "firmware" and not a.get("purged_at")][:10]
    from hil_controller.config import get_settings

    cfg = get_settings()
    return _tr(
        request,
        "job_new_firmware_bench.html",
        {
            "token": hil_token,
            "active": "jobs",
            "mcu_devices": mcu_devices,
            "recent_assets": recent_assets,
            "device_filters": {d["id"]: _device_filters(d) for d in mcu_devices},
            "cfg": {
                "protomq_repo": cfg.protomq_repo,
                "protomq_default_ref": cfg.firmware_bench_protomq_ref,
            },
            "disk": _disk_info(_jobs_dir()),
            "form": None,
            "error": None,
        },
    )


@router.get("/jobs/new-bisect", response_class=HTMLResponse, include_in_schema=False)
async def new_bisect_page(request: Request, hil_token: str = Cookie(default="")) -> HTMLResponse:
    # Before the /jobs/{job_id} catch-all (same reason as new-firmware-bench).
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    from hil_controller.config import get_settings

    db_path: str = request.app.state.db_path
    mcu_devices = [d for d in await _devices(db_path) if d["kind"] == "microcontroller"]
    cfg = get_settings()
    return _tr(
        request,
        "job_new_bisect.html",
        {
            "token": hil_token,
            "active": "jobs",
            "mcu_devices": mcu_devices,
            "secrets_configured": bool(cfg.bench_wifi_ssid),
            "form": None,
            "error": None,
        },
    )


@router.post("/jobs/bisect", include_in_schema=False, response_model=None)
async def submit_bisect(
    request: Request,
    hil_token: str = Cookie(default=""),
    device_id: Annotated[str, Form()] = "",
    working_ref: Annotated[str, Form()] = "",
    broken_ref: Annotated[str, Form()] = "",
    asset_glob: Annotated[str, Form()] = "",
    flasher: Annotated[str, Form()] = "uf2-msc",
    verify_times: Annotated[str, Form()] = "2",
    repo: Annotated[str, Form()] = "",
    test_branch: Annotated[str, Form()] = "",
    extra_cmd: Annotated[str, Form()] = "",
    io_url: Annotated[str, Form()] = "",
    io_port: Annotated[str, Form()] = "",
    io_username: Annotated[str, Form()] = "",
    io_key: Annotated[str, Form()] = "",
    wifi_ssid: Annotated[str, Form()] = "",
    wifi_password: Annotated[str, Form()] = "",
) -> Response:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    from hil_controller.bisect import WS_REPO, BisectConfig, is_cloud_broker, is_real_io_key
    from hil_controller.config import get_settings
    from hil_controller.web.bisect_runs import start_bisect

    cfg = get_settings()
    db_path: str = request.app.state.db_path
    mcu_devices = [d for d in await _devices(db_path) if d["kind"] == "microcontroller"]

    def _err(msg: str) -> HTMLResponse:
        return _tr(
            request,
            "job_new_bisect.html",
            {
                "token": hil_token,
                "active": "jobs",
                "mcu_devices": mcu_devices,
                "secrets_configured": bool(cfg.bench_wifi_ssid),
                "form": {
                    "device_id": device_id,
                    "working_ref": working_ref,
                    "broken_ref": broken_ref,
                    "asset_glob": asset_glob,
                    "flasher": flasher,
                    "verify_times": verify_times,
                    "repo": repo,
                    "test_branch": test_branch,
                    "extra_cmd": extra_cmd,
                    "io_url": io_url,
                    "io_port": io_port,
                    "io_username": io_username,
                    "wifi_ssid": wifi_ssid,
                },
                "error": msg,
            },
        )

    if not device_id or not working_ref or not broken_ref or not asset_glob:
        return _err("device, working ref, broken ref, and asset glob are all required")

    # Secret precedence: request field → controller.env config (when it's not the
    # bare placeholder) → server-side default/derivation. IO creds for a CLOUD
    # broker MUST be real; for the local broker they're left empty so the bench
    # derives anonymous per-job creds. WiFi defaults to bench-wifi/changeme.
    def _cfg_io_user() -> str:
        return cfg.bench_io_username if cfg.bench_io_username not in ("", "hil") else ""

    def _cfg_io_key() -> str:
        return cfg.bench_io_key if is_real_io_key(cfg.bench_io_key) else ""

    r_io_url = io_url.strip() or "io.adafruit.com"
    r_io_port = int((io_port or "").strip() or 8883)
    r_io_user = io_username.strip() or _cfg_io_user()
    r_io_key = io_key.strip() or _cfg_io_key()
    r_wifi_ssid = wifi_ssid.strip() or cfg.bench_wifi_ssid or "bench-wifi"
    r_wifi_password = wifi_password.strip() or cfg.bench_wifi_password or "changeme"

    if is_cloud_broker(r_io_url) and not is_real_io_key(r_io_key):
        return _err(
            f"the cloud broker {r_io_url!r} needs a real Adafruit IO account — enter IO "
            "username + key below (the controller's configured value is a placeholder). "
            "Or set a local broker (clear the IO URL) to use anonymous per-job creds."
        )

    secrets: dict[str, str] = {"WIFI_SSID": r_wifi_ssid, "WIFI_PASSWORD": r_wifi_password}
    if r_io_user:
        secrets["IO_USERNAME"] = r_io_user
    if r_io_key:
        secrets["IO_KEY"] = r_io_key

    bcfg = BisectConfig(
        device_id=device_id,
        working_ref=working_ref,
        broken_ref=broken_ref,
        asset_glob=asset_glob,
        base_url=f"http://127.0.0.1:{cfg.port}",
        token=hil_token,  # the operator's bearer — valid regardless of static/hashed tokens
        repo=repo or WS_REPO,
        flasher=flasher,
        secrets=secrets,
        io_url=r_io_url,
        io_port=r_io_port,
        verify_times=int(verify_times or 2),
    )
    summary = {
        "device_id": device_id,
        "working_ref": working_ref,
        "broken_ref": broken_ref,
        "asset_glob": asset_glob,
        "flasher": flasher,
        "verify_times": verify_times,
        "test_branch": test_branch,
        "extra_cmd": extra_cmd,
        "io_url": r_io_url,
        "io_port": r_io_port,
        "broker": "cloud" if is_cloud_broker(r_io_url) else "local",
    }
    run_id = await start_bisect(bcfg, summary)
    return RedirectResponse(f"/ui/bisect/{run_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/bisect/{run_id}", response_class=HTMLResponse, include_in_schema=False)
async def bisect_run_page(
    request: Request, run_id: str, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    from hil_controller.web.bisect_runs import get_run

    run = get_run(run_id)
    if run is None:
        return HTMLResponse("<h1>Bisection run not found</h1>", status_code=404)
    return _tr(
        request,
        "bisect_run.html",
        {"token": hil_token, "active": "jobs", "run": run},
    )


@router.get("/bisect/{run_id}/log", response_class=HTMLResponse, include_in_schema=False)
async def bisect_run_log(
    request: Request, run_id: str, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    """HTMX poll target: the run's live log + status (auto-stops polling when terminal)."""
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    from hil_controller.web.bisect_runs import get_run

    run = get_run(run_id)
    if run is None:
        return HTMLResponse("run not found", status_code=404)
    return _tr(request, "bisect_log.html", {"run": run})


# Must be declared BEFORE the "/jobs/{job_id}" route below, or FastAPI matches
# "new-arduino" as a job_id and the page 404s (same reason new-arduino-ws and
# new-firmware-bench live up here).
@router.get("/jobs/new-arduino", response_class=HTMLResponse, include_in_schema=False)
async def new_arduino_job_page(
    request: Request, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    mcu_devices = [d for d in await _devices(db_path) if d["kind"] == "microcontroller"]
    assets = await _asset_rows(db_path)
    recent_assets = [a for a in assets if a["kind"] == "firmware" and not a.get("purged_at")][:10]
    return _tr(
        request,
        "job_new_arduino.html",
        {
            "token": hil_token,
            "active": "jobs",
            "mcu_devices": mcu_devices,
            "recent_assets": recent_assets,
            "disk": _disk_info(_jobs_dir()),
            "form": None,
            "error": None,
        },
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse, include_in_schema=False)
async def job_detail(
    request: Request, job_id: str, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        from hil_controller.db.connection import get_events_since, get_job

        row = await get_job(db, job_id)
        if row is None:
            return HTMLResponse("Job not found", status_code=404)
        events = await get_events_since(db, job_id, -1)
        async with db.execute(
            "SELECT * FROM assets WHERE job_id = ? AND purged_at IS NULL ORDER BY created_at",
            (job_id,),
        ) as cur:
            asset_rows = await cur.fetchall()

    j = dict(row)
    req = json.loads(j.get("request_json") or "{}")
    src = (req.get("payload") or {}).get("source") or {}
    j["repo_url"] = src.get("repo", "")
    j["ref"] = src.get("ref", "")
    j["duration"] = _duration(j.get("started_at"), j.get("finished_at"))
    meta = req.get("metadata") or {}
    j["rerun_of"] = meta.get("rerun_of")

    # Other instances spawned from this job (reruns link back via metadata).
    async with get_db(db_path) as db:
        async with db.execute(
            "SELECT id, created_at, state, result FROM jobs "
            "WHERE request_json LIKE ? AND id != ? ORDER BY created_at DESC",
            (f'%"rerun_of": "{job_id}"%', job_id),
        ) as cur:
            reruns = [dict(r) for r in await cur.fetchall()]

    assets = []
    for r in asset_rows:
        a = dict(r)
        a["size_fmt"] = _fmt_bytes(a.get("size_bytes") or 0)
        assets.append(a)

    return _tr(
        request,
        "job_detail.html",
        {
            "token": hil_token,
            "active": "jobs",
            "job": j,
            "assets": assets,
            "reruns": reruns,
            "log_html": _render_events(events),
        },
    )


@router.get("/jobs/{job_id}/log", response_class=HTMLResponse, include_in_schema=False)
async def job_log_partial(
    request: Request, job_id: str, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return HTMLResponse("", status_code=401)
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        from hil_controller.db.connection import get_events_since, get_job

        row = await get_job(db, job_id)
        if row is None:
            return HTMLResponse("Job not found", status_code=404)
        events = await get_events_since(db, job_id, -1)
    return HTMLResponse(_render_events(events))


@router.post("/jobs/{job_id}/rerun", include_in_schema=False, response_model=None)
async def rerun_job(request: Request, job_id: str, hil_token: str = Cookie(default="")) -> Response:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        from hil_controller.db.connection import get_job

        row = await get_job(db, job_id)
    if row is None:
        return HTMLResponse("Job not found", status_code=404)
    original_req = json.loads(row["request_json"])
    # Link the new instance back to its source (and to the lineage root) so the
    # rerun shows as its own job at the new run time but is traceable.
    meta = dict(original_req.get("metadata") or {})
    meta["rerun_of"] = job_id
    meta["rerun_root"] = meta.get("rerun_root") or job_id
    original_req["metadata"] = meta
    try:
        resp = await _call_jobs_api(request, original_req, hil_token)
        new_id = resp["id"]
    except Exception as exc:
        return HTMLResponse(f'<div class="alert alert-error">{html.escape(str(exc))}</div>')
    return RedirectResponse(f"/ui/jobs/{new_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/jobs/{job_id}/cancel", include_in_schema=False, response_model=None)
async def cancel_job_web(
    request: Request, job_id: str, hil_token: str = Cookie(default="")
) -> Response:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=request.app), base_url="http://test") as c:
        await c.post(
            f"/v1/jobs/{job_id}/cancel",
            headers={"Authorization": f"Bearer {hil_token}"},
        )
    return RedirectResponse(f"/ui/jobs/{job_id}", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------


@router.get("/assets", response_class=HTMLResponse, include_in_schema=False)
async def assets_page(request: Request, hil_token: str = Cookie(default="")) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    assets = await _asset_rows(db_path)
    jdir = _jobs_dir()
    total_bytes = sum(a.get("size_bytes") or 0 for a in assets if not a.get("purged_at"))
    eligible = sum(1 for a in assets if not a.get("purged_at") and a.get("purge_at"))
    return _tr(
        request,
        "assets.html",
        {
            "token": hil_token,
            "active": "assets",
            "assets": assets,
            "total_size": _fmt_bytes(total_bytes),
            "purge_eligible": eligible,
            "disk": _disk_info(jdir),
        },
    )


@router.get("/assets/{asset_id}/view", include_in_schema=False, response_model=None)
async def view_asset(
    request: Request, asset_id: str, hil_token: str = Cookie(default="")
) -> Response:
    """Serve a stored asset. Logs render inline as text/plain; other kinds
    download. URL-only assets redirect to their source."""
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        async with db.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)) as cur:
            row = await cur.fetchone()
    if row is None:
        return HTMLResponse("Asset not found", status_code=404)
    a = dict(row)
    if a.get("purged_at"):
        return HTMLResponse("Asset has been purged", status_code=410)
    if a.get("url") and not a.get("path"):
        return RedirectResponse(a["url"])
    path = a.get("path")
    if not path or not Path(path).exists():
        return HTMLResponse("Asset file is missing on disk", status_code=404)
    if a.get("kind") == "log":
        return FileResponse(path, media_type="text/plain")
    return FileResponse(
        path, media_type="application/octet-stream", filename=a.get("filename") or "asset"
    )


@router.delete("/assets/{asset_id}", response_class=HTMLResponse, include_in_schema=False)
async def purge_asset(
    request: Request, asset_id: str, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        async with db.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return HTMLResponse("", status_code=404)
        path = row["path"]
        if path and Path(path).exists():
            try:
                Path(path).unlink()
            except Exception:
                pass
        await db.execute(
            "UPDATE assets SET purged_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), asset_id),
        )
        await db.commit()
    return HTMLResponse("")


@router.post("/assets/purge-eligible", response_class=HTMLResponse, include_in_schema=False)
async def purge_eligible(request: Request, hil_token: str = Cookie(default="")) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    now = datetime.now(UTC).isoformat()
    async with get_db(db_path) as db:
        async with db.execute(
            "SELECT id, path FROM assets WHERE purge_at IS NOT NULL AND purge_at <= ? AND purged_at IS NULL",  # noqa: E501
            (now,),
        ) as cur:
            rows = await cur.fetchall()
        for r in rows:
            if r["path"] and Path(r["path"]).exists():
                try:
                    Path(r["path"]).unlink()
                except Exception:
                    pass
            await db.execute("UPDATE assets SET purged_at = ? WHERE id = ?", (now, r["id"]))
        await db.commit()
    assets = await _asset_rows(db_path)
    return _tr(request, "assets_body.html", {"assets": assets})


# ---------------------------------------------------------------------------
# Arduino WipperSnapper Test job
# ---------------------------------------------------------------------------


_ARDUINO_WS_DEFAULTS = {
    "wippersnapper_ref": "displays-v2",
    "protomq_ref": "displays-v2-testing",
    "setup": (
        "pip install -e . && pip install -e protomq/ && "
        "cd protomq && npm ci && cp .env.example.json .env.json && "
        "npm run import-protos && npm run build-web && cd .."
    ),
    "test_cmd": ". .venv/bin/activate && python -m pytest tests/ -v --tb=short",
}


def _default_build_steps(pio_env: str, serial_port: str = "") -> str:
    """Venv-first PlatformIO **compile-only** steps for the editable Build steps box.

    Compile and flash are now distinct phases (per-phase execution-location): the
    build runs on the chosen build host (the controller compiles WipperSnapper,
    which rpi-displays cannot), and the upload happens in a separate flash phase
    against the DUT — over usbip from the controller, or via shipped artifacts.
    So these steps deliberately stop at ``pio run`` and carry no ``--target
    upload`` (``serial_port`` is accepted for signature compat but unused here).

    The venv is created with --system-site-packages so pip works under PEP 668
    (Debian externally-managed) while still seeing apt-installed packages.
    """
    import shlex as _shlex

    env = _shlex.quote(pio_env)
    return (
        "python3 -m venv --system-site-packages .venv && "
        ". .venv/bin/activate && "
        "pip install platformio && "
        f"pio run -e {env}"
    )


def _build_arduino_ws_job_request(
    *,
    wippersnapper_repo: str,
    wippersnapper_ref: str,
    protomq_repo: str,
    protomq_ref: str,
    pat: str,
    submodules: bool,
    pio_env: str,
    serial_port: str,
    setup: str,
    test_cmd: str,
    protomq_script: str,
    device_id: str,
    secrets_profile: str,
    mqtt_host: str,
    mqtt_port: str,
    io_username: str,
    io_key: str,
    timeout_total: int,
    timeout_run: int,
    timeout_deploy: int,
    build_steps: str = "",
    build_host: str = "controller",
    flash_mode: str = "usbip",
    test_host: str = "none",
    protomq_host: str = "controller",
    controller_ip: str = "",
) -> dict:
    import shlex as _shlex

    proto_clone = (
        "git clone --depth 1"
        + (" --recurse-submodules" if submodules else "")
        + f" --branch {_shlex.quote(protomq_ref)} "
        + f"{_shlex.quote(protomq_repo)} protomq"
    )
    pio_steps = build_steps.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not pio_steps:
        pio_steps = _default_build_steps(pio_env, serial_port)
    extra = setup.replace("\r\n", "\n").replace("\r", "\n").strip()
    full_setup = proto_clone + " && " + pio_steps + (" && " + extra if extra else "")

    source: dict = {
        "repo": wippersnapper_repo,
        "ref": wippersnapper_ref,
        "shallow": True,
        "submodules": submodules,
        "setup": ["bash", "-c", full_setup],
    }
    if pat:
        source["pat"] = pat

    _mqtt_port = int(mqtt_port) if mqtt_port.strip().isdigit() else 1884

    # When protomq runs on the controller, the DUT firmware must reach the
    # broker at the controller's LAN IP (not 127.0.0.1) — and the observer
    # connects there too.
    effective_mqtt_host = mqtt_host
    if protomq_host == "controller" and controller_ip:
        effective_mqtt_host = controller_ip

    params: dict = {
        "entry": "bash",
        "args": ["-c", test_cmd.replace("\r\n", "\n").replace("\r", "\n")],
        "protomq_ref": protomq_ref,
        "secrets_format": "dotenv",
        "exec": {
            "build_host": build_host,
            "flash_mode": flash_mode,
            "test_host": test_host,
            "protomq_host": protomq_host,
            "pio_env": pio_env,
        },
    }
    if protomq_script and effective_mqtt_host:
        params["protomq"] = {
            "broker_host": effective_mqtt_host,
            "mqtt_port": _mqtt_port,
            "api_port": 5173,
            "script": protomq_script,
        }

    target: dict = {}
    if device_id:
        target["device"] = {"id": device_id}
    else:
        target["device"] = {"kind": "microcontroller", "capabilities": ["wippersnapper"]}

    secrets: dict = {"MQTT_HOST": effective_mqtt_host, "MQTT_PORT": str(_mqtt_port)}
    if io_username:
        secrets["IO_USERNAME"] = io_username
    if io_key:
        secrets["IO_KEY"] = io_key

    return {
        "target": target,
        "script": "pytest-suite",
        "payload": {"kind": "git-source", "source": source},
        "params": params,
        "secrets": secrets,
        "secrets_profile": secrets_profile,
        "metadata": {"wippersnapper_ref": wippersnapper_ref, "protomq_ref": protomq_ref},
        "timeouts": {
            "total_s": timeout_total,
            "deploy_s": timeout_deploy,
            "run_s": timeout_run,
            "flash_s": 300,
        },
    }


# ---------------------------------------------------------------------------
# Arduino job
# ---------------------------------------------------------------------------


@router.post("/jobs/arduino", include_in_schema=False, response_model=None)
async def submit_arduino_job(
    request: Request,
    hil_token: str = Cookie(default=""),
    firmware_source: Annotated[str, Form()] = "url",
    firmware_url: Annotated[str, Form()] = "",
    reuse_asset_id: Annotated[str, Form()] = "",
    flasher: Annotated[str, Form()] = "esptool",
    flash_args: Annotated[str, Form()] = "",
    purge_days: Annotated[str, Form()] = "30",
    device_id: Annotated[str, Form()] = "",
    pool: Annotated[str, Form()] = "public",
    timeout_flash: Annotated[str, Form()] = "120",
    timeout_total: Annotated[str, Form()] = "300",
    firmware_file: UploadFile | None = File(default=None),
) -> Response:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()

    db_path: str = request.app.state.db_path
    mcu_devices = [d for d in await _devices(db_path) if d["kind"] == "microcontroller"]
    assets = await _asset_rows(db_path)
    recent_assets = [a for a in assets if a["kind"] == "firmware" and not a.get("purged_at")][:10]
    jdir = _jobs_dir()

    form_vals = {
        "firmware_source": firmware_source,
        "firmware_url": firmware_url,
        "reuse_asset_id": reuse_asset_id,
        "flasher": flasher,
        "flash_args": flash_args,
        "purge_days": purge_days,
        "device_id": device_id,
        "pool": pool,
        "timeout_flash": timeout_flash,
        "timeout_total": timeout_total,
    }

    def _err(msg: str) -> HTMLResponse:
        return _tr(
            request,
            "job_new_arduino.html",
            {
                "token": hil_token,
                "active": "jobs",
                "mcu_devices": mcu_devices,
                "recent_assets": recent_assets,
                "disk": _disk_info(jdir),
                "form": form_vals,
                "error": msg,
            },
        )

    asset_id: str | None = None
    resolved_url: str = ""
    resolved_path: str = ""

    if firmware_source == "upload":
        if reuse_asset_id:
            # reuse existing asset
            async with get_db(db_path) as db:
                async with db.execute(
                    "SELECT * FROM assets WHERE id = ?", (reuse_asset_id,)
                ) as cur:
                    existing = await cur.fetchone()
            if not existing:
                return _err("Selected asset not found")
            asset_id = existing["id"]
            resolved_path = existing["path"]
            resolved_url = existing["url"] or ""
        elif firmware_file and firmware_file.filename:
            # save uploaded file
            aid = str(uuid.uuid4())
            save_dir = Path(jdir) / "firmware" / aid
            save_dir.mkdir(parents=True, exist_ok=True)
            dest = save_dir / firmware_file.filename
            content = await firmware_file.read()
            dest.write_bytes(content)
            size = len(content)
            days = int(purge_days or 0)
            purge_at = None
            if days:
                from datetime import timedelta

                purge_at = (datetime.now(UTC) + timedelta(days=days)).isoformat()
            async with get_db(db_path) as db:
                await db.execute(
                    """INSERT INTO assets
                       (id, filename, path, size_bytes, kind, job_id, created_at, purge_at)
                       VALUES (?, ?, ?, ?, 'firmware', NULL, ?, ?)""",
                    (
                        aid,
                        firmware_file.filename,
                        str(dest),
                        size,
                        datetime.now(UTC).isoformat(),
                        purge_at,
                    ),
                )
                await db.commit()
            asset_id = aid
            resolved_path = str(dest)
        else:
            return _err("Select a file to upload or choose a previously uploaded firmware")
    else:
        if not firmware_url:
            return _err("Firmware URL is required")
        # store as URL-only asset (no local file)
        aid = str(uuid.uuid4())
        fname = Path(firmware_url.split("?")[0]).name or "firmware.bin"
        async with get_db(db_path) as db:
            await db.execute(
                """INSERT INTO assets (id, filename, url, size_bytes, kind, job_id, created_at)
                   VALUES (?, ?, ?, 0, 'firmware', NULL, ?)""",
                (aid, fname, firmware_url, datetime.now(UTC).isoformat()),
            )
            await db.commit()
        asset_id = aid
        resolved_url = firmware_url

    target: dict = {"pool": pool}
    if device_id:
        target["device"] = {"id": device_id}
    else:
        target["device"] = {"kind": "microcontroller"}

    extra_flash = shlex.split(flash_args) if flash_args.strip() else []
    job_req = {
        "target": target,
        "script": "firmware-flash",
        "payload": {
            "kind": "firmware-binary",
            "source": {
                "asset_id": asset_id,
                "url": resolved_url,
                "path": resolved_path,
                "flasher": flasher,
            },
        },
        "params": {"flasher": flasher, "flash_args": extra_flash},
        "timeouts": {
            "total_s": int(timeout_total or 300),
            "flash_s": int(timeout_flash or 120),
            "run_s": 60,
            "deploy_s": 60,
        },
    }

    try:
        resp = await _call_jobs_api(request, job_req, hil_token)
        job_id = resp["id"]
        # link asset to job
        async with get_db(db_path) as db:
            await db.execute("UPDATE assets SET job_id = ? WHERE id = ?", (job_id, asset_id))
            await db.commit()
    except Exception as exc:
        return _err(str(exc))

    return RedirectResponse(f"/ui/jobs/{job_id}", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Firmware-bench — interactive flash + protomq hold session
# ---------------------------------------------------------------------------


def _device_filters(device: dict) -> dict[str, str]:
    """Best-effort read of a DUT's assigned port/MSC filters from usb_json.

    The usbip page is where these get assigned; until then they're blank and the
    operator types them on the form. Stored inside ``usb_json`` so no schema
    migration is needed.
    """
    import json

    raw = device.get("usb_json")
    data: dict = {}
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except ValueError:
            data = {}
    elif isinstance(raw, dict):
        data = raw
    # Top-level device fields (from topology) take precedence over usb_json.
    return {
        "flash_port_filter": str(
            device.get("flash_port_filter") or data.get("flash_port_filter", "")
        ),
        "log_port_filter": str(device.get("log_port_filter") or data.get("log_port_filter", "")),
        "msc_filter": str(device.get("msc_filter") or data.get("msc_filter", "")),
    }


def _firmware_bench_stages_from_form(form: dict) -> list[dict]:
    """Assemble the stage list from the canonical checkboxes, in fixed order.

    An ``advanced_stages`` JSON blob, when present, overrides this entirely.
    """
    import json

    advanced = (form.get("advanced_stages") or "").strip()
    if advanced:
        parsed = json.loads(advanced)  # ValueError surfaces as a form error
        if not isinstance(parsed, list):
            raise ValueError("advanced stages must be a JSON list")
        return parsed

    offset = form.get("offset") or "0x0"
    off_s = float(form.get("power_off_s") or 1.0)
    stages: list[dict] = []
    if form.get("stage_enter_bootloader"):
        stages.append({"type": "enter_bootloader"})
    if form.get("stage_touch"):
        stages.append(
            {"type": "bootloader_touch", "settle_s": float(form.get("touch_settle_s") or 2.0)}
        )
    # Every esptool step keeps the chip in the ROM: --after no_reset always (so a
    # probe/erase/flash/verify never bounces the device out of download mode —
    # the only reboot is the deliberate power-cycle stage), and --before no_reset
    # once it's in the ROM (via enter_bootloader or a touch), since a native-USB
    # S3's USB-Serial/JTAG drops out if esptool toggles its default reset.
    in_rom = bool(form.get("stage_enter_bootloader") or form.get("stage_touch"))
    noreset = {"after": "no_reset", **({"before": "no_reset"} if in_rom else {})}
    if form.get("stage_erase"):
        stages.append({"type": "erase", **noreset})
    if form.get("stage_flash"):
        stages.append(
            {
                "type": "flash",
                "offset": offset,
                "flasher": form.get("flasher") or "esptool",
                **noreset,
            }
        )
    if form.get("stage_verify"):
        stages.append({"type": "verify", "offset": offset, **noreset})
    # Power-cycle BEFORE secrets so the app boots and its MSC volume enumerates.
    if form.get("stage_power_boot"):
        stages.append({"type": "power_cycle", "off_s": off_s, "settle_s": 3.0})
    if form.get("stage_secrets"):
        stages.append({"type": "write_secrets_msc"})
    # Power-cycle AFTER to apply the new secrets / boot the flashed app.
    if form.get("stage_power_final"):
        stages.append({"type": "power_cycle", "off_s": off_s})
    return stages


def _build_firmware_bench_job_request(
    *,
    device_id: str,
    pool: str,
    firmware_path: str,
    offset: str,
    stages: list[dict],
    window_minutes: int,
    flash_port_filter: str,
    log_port_filter: str,
    msc_filter: str,
    flash_serial_port: str,
    log_serial_port: str,
    esptool_chip: str,
    esptool_baud: int,
    serial_baud: int,
    protomq_repo: str,
    protomq_ref: str,
    protomq_script: str,
    secrets_profile: str,
    io_username: str,
    io_key: str,
    wifi_ssid: str,
    wifi_password: str,
) -> dict:
    target: dict = {"pool": pool}
    if device_id:
        target["device"] = {"id": device_id}
    else:
        target["device"] = {"kind": "microcontroller", "capabilities": ["wippersnapper"]}

    firmware = {"path": firmware_path, "offset": offset}
    params: dict = {
        "firmware": firmware,
        "stages": stages,
        "window_minutes": window_minutes,
        "flash_port_filter": flash_port_filter,
        "log_port_filter": log_port_filter,
        "msc_filter": msc_filter,
        "flash_serial_port": flash_serial_port,
        "log_serial_port": log_serial_port,
        "esptool_chip": esptool_chip,
        "esptool_baud": esptool_baud,
        "serial_baud": serial_baud,
        "protomq_repo": protomq_repo,
        "protomq_ref": protomq_ref,
        "protomq_script": protomq_script,
    }

    secrets: dict = {}
    if io_username:
        secrets["IO_USERNAME"] = io_username
    if io_key:
        secrets["IO_KEY"] = io_key
    if wifi_ssid:
        secrets["WIFI_SSID"] = wifi_ssid
    if wifi_password:
        secrets["WIFI_PASSWORD"] = wifi_password

    return {
        "target": target,
        "script": "firmware-bench",
        "payload": {"kind": "firmware-bin", "firmware": firmware},
        "params": params,
        "secrets": secrets,
        "secrets_profile": secrets_profile,
        # total_s is not the deadline for interactive holds (the worker skips its
        # wait_for); set a generous ceiling for the clone+build+flash setup phase.
        "timeouts": {"total_s": 7200, "deploy_s": 1800, "run_s": 3600, "flash_s": 600},
    }


@router.post("/jobs/firmware-bench", include_in_schema=False, response_model=None)
async def submit_firmware_bench_job(
    request: Request,
    hil_token: str = Cookie(default=""),
    device_id: Annotated[str, Form()] = "",
    pool: Annotated[str, Form()] = "public",
    firmware_source: Annotated[str, Form()] = "upload",
    firmware_path: Annotated[str, Form()] = "",
    reuse_asset_id: Annotated[str, Form()] = "",
    offset: Annotated[str, Form()] = "0x0",
    window_minutes: Annotated[str, Form()] = "30",
    # Checkbox fields default to "" — an unchecked box sends nothing, so a non-empty
    # default would make it impossible to uncheck. Initial-checked state is set by
    # the template for a fresh (form=None) render.
    stage_enter_bootloader: Annotated[str, Form()] = "",
    stage_touch: Annotated[str, Form()] = "",
    touch_settle_s: Annotated[str, Form()] = "2.0",
    stage_erase: Annotated[str, Form()] = "",
    stage_flash: Annotated[str, Form()] = "",
    flasher: Annotated[str, Form()] = "esptool",
    stage_verify: Annotated[str, Form()] = "",
    stage_power_boot: Annotated[str, Form()] = "",
    stage_secrets: Annotated[str, Form()] = "",
    stage_power_final: Annotated[str, Form()] = "",
    power_off_s: Annotated[str, Form()] = "1.0",
    advanced_stages: Annotated[str, Form()] = "",
    flash_port_filter: Annotated[str, Form()] = "",
    log_port_filter: Annotated[str, Form()] = "",
    msc_filter: Annotated[str, Form()] = "",
    flash_serial_port: Annotated[str, Form()] = "",
    log_serial_port: Annotated[str, Form()] = "",
    esptool_chip: Annotated[str, Form()] = "auto",
    esptool_baud: Annotated[str, Form()] = "921600",
    serial_baud: Annotated[str, Form()] = "115200",
    protomq_repo: Annotated[str, Form()] = "",
    protomq_ref: Annotated[str, Form()] = "",
    protomq_script: Annotated[str, Form()] = "",
    secrets_profile: Annotated[str, Form()] = "bench-protomq",
    io_username: Annotated[str, Form()] = "",
    io_key: Annotated[str, Form()] = "",
    wifi_ssid: Annotated[str, Form()] = "",
    wifi_password: Annotated[str, Form()] = "",
    firmware_file: UploadFile | None = File(default=None),
) -> Response:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()

    db_path: str = request.app.state.db_path
    mcu_devices = [d for d in await _devices(db_path) if d["kind"] == "microcontroller"]
    assets = await _asset_rows(db_path)
    recent_assets = [a for a in assets if a["kind"] == "firmware" and not a.get("purged_at")][:10]
    jdir = _jobs_dir()

    form_vals = {
        "device_id": device_id,
        "pool": pool,
        "firmware_source": firmware_source,
        "firmware_path": firmware_path,
        "reuse_asset_id": reuse_asset_id,
        "offset": offset,
        "window_minutes": window_minutes,
        "stage_enter_bootloader": stage_enter_bootloader,
        "stage_touch": stage_touch,
        "touch_settle_s": touch_settle_s,
        "stage_erase": stage_erase,
        "stage_flash": stage_flash,
        "flasher": flasher,
        "stage_verify": stage_verify,
        "stage_power_boot": stage_power_boot,
        "stage_secrets": stage_secrets,
        "stage_power_final": stage_power_final,
        "power_off_s": power_off_s,
        "advanced_stages": advanced_stages,
        "flash_port_filter": flash_port_filter,
        "log_port_filter": log_port_filter,
        "msc_filter": msc_filter,
        "flash_serial_port": flash_serial_port,
        "log_serial_port": log_serial_port,
        "esptool_chip": esptool_chip,
        "esptool_baud": esptool_baud,
        "serial_baud": serial_baud,
        "protomq_repo": protomq_repo,
        "protomq_ref": protomq_ref,
        "protomq_script": protomq_script,
        "secrets_profile": secrets_profile,
        "io_username": io_username,
        "wifi_ssid": wifi_ssid,  # never echo io_key / wifi_password back into the form
    }
    from hil_controller.config import get_settings

    cfg = get_settings()

    def _err(msg: str) -> HTMLResponse:
        return _tr(
            request,
            "job_new_firmware_bench.html",
            {
                "token": hil_token,
                "active": "jobs",
                "mcu_devices": mcu_devices,
                "recent_assets": recent_assets,
                "device_filters": {d["id"]: _device_filters(d) for d in mcu_devices},
                "cfg": {
                    "protomq_repo": cfg.protomq_repo,
                    "protomq_default_ref": cfg.firmware_bench_protomq_ref,
                },
                "disk": _disk_info(jdir),
                "form": form_vals,
                "error": msg,
            },
        )

    # Resolve the firmware .bin → a path on the controller.
    resolved_path = ""
    asset_id: str | None = None
    if firmware_source == "path":
        if not firmware_path:
            return _err("Firmware path is required")
        resolved_path = firmware_path
    elif reuse_asset_id:
        async with get_db(db_path) as db:
            async with db.execute("SELECT * FROM assets WHERE id = ?", (reuse_asset_id,)) as cur:
                existing = await cur.fetchone()
        if not existing:
            return _err("Selected asset not found")
        asset_id = existing["id"]
        resolved_path = existing["path"]
    elif firmware_file and firmware_file.filename:
        aid = str(uuid.uuid4())
        save_dir = Path(jdir) / "firmware" / aid
        save_dir.mkdir(parents=True, exist_ok=True)
        dest = save_dir / firmware_file.filename
        content = await firmware_file.read()
        dest.write_bytes(content)
        async with get_db(db_path) as db:
            await db.execute(
                """INSERT INTO assets (id, filename, path, size_bytes, kind, job_id, created_at)
                   VALUES (?, ?, ?, ?, 'firmware', NULL, ?)""",
                (
                    aid,
                    firmware_file.filename,
                    str(dest),
                    len(content),
                    datetime.now(UTC).isoformat(),
                ),
            )
            await db.commit()
        asset_id = aid
        resolved_path = str(dest)
    else:
        return _err("Upload a .bin, choose a previous upload, or give a server path")

    try:
        stages = _firmware_bench_stages_from_form(form_vals)
    except ValueError as exc:
        return _err(f"Invalid advanced stages JSON: {exc}")
    if not stages:
        return _err("Select at least one stage")

    job_req = _build_firmware_bench_job_request(
        device_id=device_id,
        pool=pool,
        firmware_path=resolved_path,
        offset=offset,
        stages=stages,
        window_minutes=int(window_minutes or 30),
        flash_port_filter=flash_port_filter,
        log_port_filter=log_port_filter,
        msc_filter=msc_filter,
        flash_serial_port=flash_serial_port,
        log_serial_port=log_serial_port,
        esptool_chip=esptool_chip,
        esptool_baud=int(esptool_baud or 921600),
        serial_baud=int(serial_baud or 115200),
        protomq_repo=protomq_repo,
        protomq_ref=protomq_ref,
        protomq_script=protomq_script,
        secrets_profile=secrets_profile,
        io_username=io_username,
        io_key=io_key,
        wifi_ssid=wifi_ssid,
        wifi_password=wifi_password,
    )

    try:
        resp = await _call_jobs_api(request, job_req, hil_token)
        job_id = resp["id"]
        if asset_id:
            async with get_db(db_path) as db:
                await db.execute("UPDATE assets SET job_id = ? WHERE id = ?", (job_id, asset_id))
                await db.commit()
    except Exception as exc:
        return _err(str(exc))

    return RedirectResponse(f"/ui/jobs/{job_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/jobs/{job_id}/extend", include_in_schema=False, response_model=None)
async def extend_firmware_bench_job(
    request: Request,
    job_id: str,
    hil_token: str = Cookie(default=""),
    minutes: Annotated[str, Form()] = "30",
) -> Response:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    try:
        await _call_jobs_api_path(
            request, f"/v1/jobs/{job_id}/extend", {"minutes": int(minutes or 30)}, hil_token
        )
    except Exception:
        pass
    return RedirectResponse(f"/ui/jobs/{job_id}", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Leases — runtime visibility for device/hub exclusivity locks
# ---------------------------------------------------------------------------


async def _leases_with_context(db_path: str, *, active_only: bool = True) -> list[dict]:
    """Return leases joined with device + host + job summary fields for the UI."""
    from hil_controller.queue.leases import list_active, list_all

    rows = await (list_active(db_path) if active_only else list_all(db_path))
    if not rows:
        return []
    job_ids = sorted({r["job_id"] for r in rows if r["job_id"]})
    async with get_db(db_path) as db:
        async with db.execute("SELECT id, host_id, kind, model FROM devices") as cur:
            dmap = {r["id"]: dict(r) for r in await cur.fetchall()}
        async with db.execute("SELECT id, role, addr FROM hosts") as cur:
            hmap = {r["id"]: dict(r) for r in await cur.fetchall()}
        jmap: dict[str, dict] = {}
        if job_ids:
            placeholders = ",".join("?" * len(job_ids))
            async with db.execute(
                f"SELECT id, state FROM jobs WHERE id IN ({placeholders})", job_ids
            ) as cur:
                jmap = {r["id"]: dict(r) for r in await cur.fetchall()}
    out: list[dict] = []
    for r in rows:
        dev = dmap.get(r["device_id"]) if r["device_id"] else None
        host = hmap.get(r["hub_host_id"]) if r["hub_host_id"] else None
        if dev and not host:
            host = hmap.get(dev["host_id"])
        job = jmap.get(r["job_id"]) if r["job_id"] else None
        out.append(
            {
                **r,
                "device_model": (dev or {}).get("model"),
                "device_kind": (dev or {}).get("kind"),
                "host_id": (host or {}).get("id"),
                "host_addr": (host or {}).get("addr"),
                "job_state": (job or {}).get("state"),
            }
        )
    return out


@router.get("/leases", response_class=HTMLResponse, include_in_schema=False)
async def leases_page(request: Request, hil_token: str = Cookie(default="")) -> HTMLResponse:
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()  # type: ignore[return-value]
    db_path: str = request.app.state.db_path
    leases = await _leases_with_context(db_path, active_only=True)
    return _tr(
        request,
        "leases.html",
        {"token": hil_token, "active": "leases", "leases": leases},
    )


@router.get("/leases/body", response_class=HTMLResponse, include_in_schema=False)
async def leases_body(
    request: Request,
    hil_token: str = Cookie(default=""),
    active_only: bool = True,
) -> HTMLResponse:
    """HTMX-refreshable body fragment; called every few seconds by leases.html."""
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()  # type: ignore[return-value]
    db_path: str = request.app.state.db_path
    leases = await _leases_with_context(db_path, active_only=active_only)
    return _tr(request, "leases_body.html", {"leases": leases})


@router.delete("/leases/{lease_id}", response_class=HTMLResponse, include_in_schema=False)
async def release_lease_ui(
    request: Request, lease_id: int, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    """Force-release a stuck lease from the UI (admin-only path).

    Returns an empty fragment so HTMX swaps the row out of the table.
    """
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    from hil_controller.queue.leases import release

    await release(db_path, lease_id)
    return HTMLResponse("")


# ---------------------------------------------------------------------------
# Bench-wide USB-IP overview — every host's busids on one screen
# ---------------------------------------------------------------------------


async def _hub_hosts_with_devices(db_path: str) -> list[dict]:
    """Hosts that own at least one MCU device (i.e. plausible USB-IP servers).

    Returns ``[{id, addr, role, transport, device_count}]``. Used by the
    /ui/usbip page to decide which hosts to query; falls back to "all
    hosts" if no device is recorded yet (so a brand-new bench can still
    discover its first busid).
    """
    async with get_db(db_path) as db:
        async with db.execute(
            """
            SELECT h.id, h.addr, h.role, h.transport,
                   SUM(CASE WHEN d.kind = 'microcontroller'
                            AND (d.hub_host_id = h.id OR
                                 (d.hub_host_id IS NULL AND d.host_id = h.id))
                       THEN 1 ELSE 0 END) AS device_count
            FROM hosts h
            LEFT JOIN devices d ON d.host_id = h.id OR d.hub_host_id = h.id
            GROUP BY h.id ORDER BY h.id
            """
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    matched = [r for r in rows if (r.get("device_count") or 0) > 0]
    return matched or rows


async def _busid_to_device_map_for_host(db_path: str, host_id: str) -> dict[str, str]:
    """{busid: device_id} for devices whose hub is *host_id*."""
    async with get_db(db_path) as db:
        async with db.execute(
            "SELECT id, hub_port_path FROM devices "
            "WHERE hub_host_id = ? OR (hub_host_id IS NULL AND host_id = ?)",
            (host_id, host_id),
        ) as cur:
            rows = await cur.fetchall()
    return {r["hub_port_path"]: r["id"] for r in rows if r["hub_port_path"]}


async def _host_has_arduino(db_path: str, host_id: str) -> bool:
    """True if the host advertises ``arduino`` or hosts any ``arduino`` device.

    Drives whether the USB-IP page surfaces the host's stable ``/dev`` serial
    and disk symlinks — the paths a serial-capture or UF2/MSC-flashing step
    needs. ``rpi-displays`` qualifies via its arduino-tagged MCU devices even
    though the host row itself has no ``arduino`` capability.
    """
    async with get_db(db_path) as db:
        async with db.execute(
            "SELECT capabilities_json FROM hosts WHERE id = ?", (host_id,)
        ) as cur:
            host_row = await cur.fetchone()
        if host_row and "arduino" in json.loads(host_row["capabilities_json"]):
            return True
        async with db.execute(
            "SELECT capabilities_json FROM devices WHERE host_id = ? OR hub_host_id = ?",
            (host_id, host_id),
        ) as cur:
            for r in await cur.fetchall():
                if "arduino" in json.loads(r["capabilities_json"]):
                    return True
    return False


async def _assignable_devices_for_host(db_path: str, host_id: str) -> list[dict]:
    """Devices whose hub is *host_id* and that DON'T already have a busid set.

    Returned for the "assign to..." dropdown next to unmatched busids.
    """
    async with get_db(db_path) as db:
        async with db.execute(
            "SELECT id, model, kind FROM devices "
            "WHERE (hub_host_id = ? OR (hub_host_id IS NULL AND host_id = ?)) "
            "AND (hub_port_path IS NULL OR hub_port_path = '') "
            "ORDER BY id",
            (host_id, host_id),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def _device_solenoid_map_for_host(db_path: str, host_id: str) -> dict[str, int]:
    """{device_id: solenoid_channel} for devices on *host_id* that have a channel.

    Feeds the usb-ip page's per-device power controls (On/Off/Cycle).
    """
    async with get_db(db_path) as db:
        async with db.execute(
            "SELECT id, solenoid_channel FROM devices "
            "WHERE (hub_host_id = ? OR host_id = ?) AND solenoid_channel IS NOT NULL",
            (host_id, host_id),
        ) as cur:
            return {r["id"]: r["solenoid_channel"] for r in await cur.fetchall()}


_SOLENOID_HOST_CAPS = {"mcp23017", "power-control", "solenoid", "solenoid-hub"}


async def _host_is_solenoid_capable(db_path: str, host_id: str, solenoid_map: dict) -> bool:
    """True if the host has a solenoid hub — by capability tag or a mapped channel."""
    if solenoid_map:
        return True
    async with get_db(db_path) as db:
        async with db.execute(
            "SELECT capabilities_json FROM hosts WHERE id = ?", (host_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row or not row["capabilities_json"]:
        return False
    try:
        caps = {str(c).lower() for c in json.loads(row["capabilities_json"])}
    except Exception:  # noqa: BLE001
        return False
    return bool(caps & _SOLENOID_HOST_CAPS)


@router.get("/usbip", response_class=HTMLResponse, include_in_schema=False)
async def usbip_overview(
    request: Request,
    hil_token: str = Cookie(default=""),
    timeout: float | None = Query(default=None),
) -> HTMLResponse:
    """Top-level bench overview: every host gets a placeholder section that
    HTMX-loads its busid table on connect.

    ``?timeout=<seconds>`` is an optional per-command cap propagated to each
    host fragment; with no timeout the page waits for the full result (``lsusb
    -v`` is slow on a big/wedged bus but does finish)."""
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    hosts = await _hub_hosts_with_devices(db_path)
    return _tr(
        request,
        "usbip.html",
        {"token": hil_token, "active": "usbip", "hosts": hosts, "timeout": timeout},
    )


@router.get(
    "/usbip/host/{host_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def usbip_host_fragment(
    request: Request,
    host_id: str,
    hil_token: str = Cookie(default=""),
    timeout: float | None = Query(default=None),
) -> HTMLResponse:
    """HTMX fragment: one host's busid table.

    Runs ``usbip list -l`` on the host via app.state.host_registry and
    renders rows for each busid — with an assign-to dropdown for the
    unmatched ones, listing devices whose hub is this host and that
    don't yet have a busid pinned.

    ``?timeout=<seconds>`` optionally caps each enumeration command (``lsusb
    -v`` etc.); omitted → wait for the full result.
    """
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    registry = getattr(request.app.state, "host_registry", None)
    if registry is None:
        return _tr(
            request,
            "usbip_host_fragment.html",
            {
                "host_id": host_id,
                "daemon_listening": False,
                "busids": [],
                "hub_info": [],
                "error": "host registry not loaded (HIL_TOPOLOGY_FILE unset?)",
                "assignable_devices": [],
            },
        )
    try:
        transport = registry.transport_for(host_id)
    except KeyError:
        return _tr(
            request,
            "usbip_host_fragment.html",
            {
                "host_id": host_id,
                "daemon_listening": False,
                "busids": [],
                "hub_info": [],
                "error": f"unknown host: {host_id}",
                "assignable_devices": [],
            },
        )

    from hil_controller.adapters.usbip_inventory import query_host_busids

    device_map = await _busid_to_device_map_for_host(db_path, host_id)
    wants_dev_links = await _host_has_arduino(db_path, host_id)
    inventory = await query_host_busids(
        transport,
        host_id=host_id,
        device_busid_map=device_map,
        include_dev_links=wants_dev_links,
        timeout_s=timeout,
    )
    assignable = await _assignable_devices_for_host(db_path, host_id)
    solenoid_map = await _device_solenoid_map_for_host(db_path, host_id)
    channel_labels = {ch: dev for dev, ch in solenoid_map.items()}  # bank-A channel -> device
    solenoid_capable = await _host_is_solenoid_capable(db_path, host_id, solenoid_map)
    return _tr(
        request,
        "usbip_host_fragment.html",
        {
            "host_id": inventory.host_id,
            "daemon_listening": inventory.daemon_listening,
            "busids": inventory.busids,
            "hub_info": inventory.hub_info,
            "error": inventory.error,
            "assignable_devices": assignable,
            "dev_links": inventory.dev_links,
            "device_solenoid": solenoid_map,
            "solenoid_capable": solenoid_capable,
            "solenoid_channel_labels": channel_labels,
            "solenoid_bank_a": list(range(0, 8)),
            "solenoid_bank_b": list(range(8, 16)),
        },
    )


@router.post(
    "/usbip/assign",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def usbip_assign(
    request: Request,
    host_id: str = Form(...),
    busid: str = Form(...),
    device_id: str = Form(...),
    hil_token: str = Cookie(default=""),
) -> HTMLResponse:
    """Update a device record so its hub_host_id + hub_port_path point at
    the given (host_id, busid). Idempotent — re-assigning a device to its
    current busid is a no-op. Returns the host fragment so HTMX swaps
    the whole section in (row state changes after assignment).
    """
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        async with db.execute("SELECT id FROM devices WHERE id = ?", (device_id,)) as cur:
            if (await cur.fetchone()) is None:
                return HTMLResponse(
                    f'<div class="alert alert-error">device not found: {html.escape(device_id)}</div>',  # noqa: E501
                    status_code=200,
                )
        await db.execute(
            "UPDATE devices SET hub_host_id = ?, hub_port_path = ? WHERE id = ?",
            (host_id, busid, device_id),
        )
        await db.commit()
    # Re-render the host fragment so the just-assigned busid shows its new
    # "matched device" and disappears from the assign dropdown.
    return await usbip_host_fragment(request, host_id, hil_token=hil_token)


# ---------------------------------------------------------------------------
# Bench actions on a single device — reset + tinyuf2 install
# ---------------------------------------------------------------------------


async def _device_row(db_path: str, device_id: str) -> dict | None:
    async with get_db(db_path) as db:
        async with db.execute("SELECT * FROM devices WHERE id = ?", (device_id,)) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


def _bench_result_panel(*, ok: bool, title: str, body_html: str) -> HTMLResponse:
    """Wrap a Bench-action result in a coloured alert div for the HTMX swap target."""
    cls = "alert-success" if ok else "alert-error"
    return HTMLResponse(
        f'<div class="alert {cls}" style="margin-top:0.5rem;">'
        f"<strong>{html.escape(title)}</strong><br>{body_html}"
        f"</div>"
    )


async def _solenoid_for_device(request: Request, device_id: str):
    """Resolve a device's solenoid hub+channel for a UI power action.

    Returns ``(hub, channel, hub_host_id)`` on success, or ``(None, panel)``
    where ``panel`` is a ready-to-return error alert.
    """
    db_path: str = request.app.state.db_path
    device = await _device_row(db_path, device_id)
    if device is None:
        return None, _bench_result_panel(
            ok=False, title="Device not found", body_html=html.escape(device_id)
        )
    channel = device.get("solenoid_channel")
    if channel is None:
        return None, _bench_result_panel(
            ok=False,
            title="No solenoid channel configured",
            body_html=(
                "Set <code>solenoid_channel</code> on the device record before "
                "using power controls from the UI."
            ),
        )
    hub_host_id = device.get("hub_host_id") or device.get("host_id")
    registry = getattr(request.app.state, "host_registry", None)
    if registry is None:
        return None, _bench_result_panel(
            ok=False, title="Host registry not configured", body_html="No topology loaded."
        )
    try:
        transport = registry.transport_for(hub_host_id)
    except KeyError:
        return None, _bench_result_panel(
            ok=False, title="Unknown hub host", body_html=html.escape(str(hub_host_id))
        )
    from hil_controller.adapters.solenoid_hub import SolenoidHubAdapter

    return (SolenoidHubAdapter(transport=transport), int(channel), hub_host_id), None


async def _solenoid_action(request, device_id, hil_token, verb):
    """Shared handler for the on/off/cycle solenoid UI buttons."""
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    got, err = await _solenoid_for_device(request, device_id)
    if err is not None:
        return err
    hub, channel, hub_host_id = got
    from hil_controller.adapters.solenoid_hub import SolenoidHubError

    try:
        if verb == "on":
            await hub.port_on(channel)
            title = f"Powered ON channel {channel} on {hub_host_id}"
            note = "Pressed the ON latch."
        elif verb == "off":
            await hub.port_off(channel)
            title = f"Powered OFF channel {channel} on {hub_host_id}"
            note = "Pressed the OFF latch."
        else:  # cycle
            await hub.power_cycle(channel)
            title = f"Power-cycled channel {channel} on {hub_host_id}"
            note = "Issued port_off then port_on. Allow a couple of seconds to re-enumerate."
    except (SolenoidHubError, ValueError) as exc:
        fail_title = {"on": "Power ON failed", "off": "Power OFF failed"}.get(verb, "Reset failed")
        return _bench_result_panel(ok=False, title=fail_title, body_html=html.escape(str(exc)))
    return _bench_result_panel(ok=True, title=title, body_html=note)


@router.post("/devices/{device_id}/power/on", response_class=HTMLResponse, include_in_schema=False)
async def device_power_on_ui(
    request: Request, device_id: str, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    """Solenoid power ON for a DUT (usb-ip page control)."""
    return await _solenoid_action(request, device_id, hil_token, "on")


@router.post("/devices/{device_id}/power/off", response_class=HTMLResponse, include_in_schema=False)
async def device_power_off_ui(
    request: Request, device_id: str, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    """Solenoid power OFF for a DUT (usb-ip page control)."""
    return await _solenoid_action(request, device_id, hil_token, "off")


@router.post("/devices/{device_id}/reset", response_class=HTMLResponse, include_in_schema=False)
async def device_reset_ui(
    request: Request, device_id: str, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    """Power-cycle a DUT via its hub host's solenoid channel (usb-ip page control)."""
    return await _solenoid_action(request, device_id, hil_token, "cycle")


# --------------------------------------------------------------------------- #
# Per-HOST solenoid channel controls (usb-ip page bulk/manual switching)      #
# --------------------------------------------------------------------------- #
#: MCP23017 layout: bank A (0..7) = power-latch; bank B (8..15) = Pico BOOTSEL.
SOLENOID_BANK_A = range(0, 8)
SOLENOID_BANK_B = range(8, 16)


def _solenoid_hub_for_host(request: Request, host_id: str):
    """Return ``(hub, None)`` for a host's solenoid, or ``(None, panel)`` on error."""
    registry = getattr(request.app.state, "host_registry", None)
    if registry is None:
        return None, _bench_result_panel(
            ok=False, title="Host registry not configured", body_html="No topology loaded."
        )
    try:
        transport = registry.transport_for(host_id)
    except KeyError:
        return None, _bench_result_panel(
            ok=False, title="Unknown host", body_html=html.escape(host_id)
        )
    from hil_controller.adapters.solenoid_hub import SolenoidHubAdapter

    return SolenoidHubAdapter(transport=transport), None


@router.post(
    "/hosts/{host_id}/solenoid/ch/{channel}/{action}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def host_solenoid_channel_ui(
    request: Request,
    host_id: str,
    channel: int,
    action: str,
    hil_token: str = Cookie(default=""),
) -> HTMLResponse:
    """Drive one solenoid channel on a host: action ∈ {on, off, cycle}.

    Channel is the raw MCP23017 index (0..15) — bank A power, bank B BOOTSEL.
    """
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    if not 0 <= channel <= 15:
        return _bench_result_panel(
            ok=False, title="Bad channel", body_html="channel must be 0..15"
        )
    hub, err = _solenoid_hub_for_host(request, host_id)
    if err is not None:
        return err
    from hil_controller.adapters.solenoid_hub import SolenoidHubError

    try:
        if action == "on":
            await hub.port_on(channel)
            verb = "ON"
        elif action == "off":
            await hub.port_off(channel)
            verb = "OFF"
        elif action == "cycle":
            await hub.power_cycle(channel)
            verb = "cycled"
        else:
            return _bench_result_panel(
                ok=False, title="Bad action", body_html="action must be on/off/cycle"
            )
    except (SolenoidHubError, ValueError) as exc:
        return _bench_result_panel(
            ok=False, title=f"Channel {channel} {action} failed", body_html=html.escape(str(exc))
        )
    bank = "A/power" if channel < 8 else "B/bootsel"
    return _bench_result_panel(
        ok=True, title=f"Channel {channel} ({bank}) {verb} on {host_id}", body_html=""
    )


@router.post(
    "/hosts/{host_id}/solenoid/all-off",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def host_solenoid_all_off_ui(
    request: Request, host_id: str, hil_token: str = Cookie(default="")
) -> HTMLResponse:
    """Send OFF to every solenoid channel on a host (mass switch)."""
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    hub, err = _solenoid_hub_for_host(request, host_id)
    if err is not None:
        return err
    from hil_controller.adapters.solenoid_hub import SolenoidHubError

    try:
        await hub.all_off()
    except (SolenoidHubError, ValueError) as exc:
        return _bench_result_panel(
            ok=False, title="All-off failed", body_html=html.escape(str(exc))
        )
    return _bench_result_panel(ok=True, title=f"All channels OFF on {host_id}", body_html="")


@router.post(
    "/hosts/{host_id}/solenoid/bootsel/{channel}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def host_solenoid_bootsel_ui(
    request: Request,
    host_id: str,
    channel: int,
    hil_token: str = Cookie(default=""),
) -> HTMLResponse:
    """Attempt a Pico BOOTSEL entry on bank-A power ``channel``.

    Resolves the device mapped to this power channel and uses its
    ``bootsel_channel`` (falling back to ``channel + 8``) and ``bootsel_inverted``
    polarity. Holds BOOTSEL, power-cycles the A channel (off → on) so the
    RP2040/RP2350 boots with BOOTSEL held, then releases it.
    """
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    if not 0 <= channel <= 7:
        return _bench_result_panel(
            ok=False, title="Bad channel", body_html="BOOTSEL uses a bank-A channel (0..7)"
        )
    # Look up the device on this power channel for its bootsel wiring/polarity.
    bootsel_ch, inverted = channel + 8, False
    async with get_db(request.app.state.db_path) as db:
        async with db.execute(
            "SELECT bootsel_channel, bootsel_inverted FROM devices "
            "WHERE (hub_host_id = ? OR host_id = ?) AND solenoid_channel = ?",
            (host_id, host_id, channel),
        ) as cur:
            row = await cur.fetchone()
    if row is not None:
        if row["bootsel_channel"] is not None:
            bootsel_ch = int(row["bootsel_channel"])
        inverted = bool(row["bootsel_inverted"])
    hub, err = _solenoid_hub_for_host(request, host_id)
    if err is not None:
        return err
    from hil_controller.adapters.solenoid_hub import SolenoidHubError

    # Inverted polarity: the attachment presses on OFF, so hold==off / release==on.
    hold = hub.port_off if inverted else hub.port_on
    release = hub.port_on if inverted else hub.port_off
    try:
        await hold(bootsel_ch)  # hold BOOTSEL
        await hub.power_cycle(channel, off_s=1.0, settle_s=1.5)  # cold boot with it held
        await release(bootsel_ch)  # release BOOTSEL
    except (SolenoidHubError, ValueError) as exc:
        return _bench_result_panel(
            ok=False, title=f"BOOTSEL ch {channel} failed", body_html=html.escape(str(exc))
        )
    pol = " (inverted)" if inverted else ""
    return _bench_result_panel(
        ok=True,
        title=f"BOOTSEL attempted on ch {channel} (held ch {bootsel_ch}{pol}) on {host_id}",
        body_html="Cold-booted with BOOTSEL held; check for the RPI-RP2 drive.",
    )


@router.post(
    "/devices/{device_id}/install-tinyuf2",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def install_tinyuf2_ui(
    request: Request,
    device_id: str,
    board_name: str = Form(...),
    tag: str = Form("latest"),
    fallback_board: str = Form(""),
    chip: str = Form("auto"),
    hil_token: str = Cookie(default=""),
) -> HTMLResponse:
    """Install a TinyUF2 release on the named ESP DUT (synchronous).

    Composes :class:`TinyUf2Fetcher` + :class:`UsbipBridge` +
    :class:`EsptoolFlasher`. Runs inline (blocks the HTMX request until
    the flash is complete). Submits no job — for "do this once" operator
    flows. The job-kind integration is a separate M4 task.
    """
    if not (await _check_web_token(request, hil_token)):
        return _login_redirect()
    db_path: str = request.app.state.db_path
    device = await _device_row(db_path, device_id)
    if device is None:
        return _bench_result_panel(
            ok=False, title="Device not found", body_html=html.escape(device_id)
        )
    busid = device.get("hub_port_path")
    if not busid:
        return _bench_result_panel(
            ok=False,
            title="hub_port_path not set",
            body_html=(
                "TinyUF2 install needs a usbip-bindable bus-id. Set "
                "<code>hub_port_path</code> on the device record first."
            ),
        )
    hub_host_id = device.get("hub_host_id") or device.get("host_id")
    registry = getattr(request.app.state, "host_registry", None)
    if registry is None:
        return _bench_result_panel(
            ok=False,
            title="Host registry not configured",
            body_html="No topology loaded.",
        )
    try:
        dut_transport = registry.transport_for(hub_host_id)
    except KeyError:
        return _bench_result_panel(
            ok=False,
            title="Unknown hub host",
            body_html=html.escape(str(hub_host_id)),
        )

    # Look up the hub host's address so usbip attach can reach the daemon.
    async with get_db(db_path) as db:
        async with db.execute("SELECT addr FROM hosts WHERE id = ?", (hub_host_id,)) as cur:
            host_row = await cur.fetchone()
    server_addr = host_row["addr"] if host_row else hub_host_id

    from hil_controller.adapters.flashers import FlasherToolFailed
    from hil_controller.adapters.tinyuf2_install import (
        TinyUf2Installer,
        TinyUf2InstallError,
    )
    from hil_controller.hosts.local import LocalTransport

    installer = TinyUf2Installer(
        controller_transport=LocalTransport(),
        dut_transport=dut_transport,
        server_addr=server_addr,
        busid=busid,
        board_name=board_name.strip(),
        esptool_chip=chip.strip() or "auto",
    )
    try:
        result = await installer.install(
            tag=tag.strip() or "latest",
            fallback_board=fallback_board.strip() or None,
        )
    except (TinyUf2InstallError, FlasherToolFailed, FileNotFoundError) as exc:
        return _bench_result_panel(
            ok=False,
            title="TinyUF2 install failed",
            body_html=f"<pre style='white-space:pre-wrap;'>{html.escape(str(exc))}</pre>",
        )
    return _bench_result_panel(
        ok=True,
        title=(
            f"TinyUF2 {html.escape(result.tag)} installed on "
            f"{html.escape(board_name)} (busid {html.escape(busid)})"
        ),
        body_html=(
            f"asset <code>{html.escape(result.asset_name)}</code><br>"
            f"sha256 <code>{result.digest_sha256[:16]}…</code><br>"
            f"serial port <code>{html.escape(result.serial_port)}</code><br>"
            f"wrote <strong>{result.bytes_written}</strong> bytes in "
            f"{result.elapsed_s:.1f}s<br>"
            f"Next: the DUT should re-enumerate as a UF2 MSC drive."
        ),
    )
