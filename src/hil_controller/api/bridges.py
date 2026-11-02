"""/v1/bridges — esp-usbip-bridge discovery proposals and cached capabilities.

* ``GET  /v1/bridges`` — every bridge host with its cached capabilities.
* ``GET  /v1/bridges/{host_id}/capabilities[?refresh=true]`` — one host's view.
* ``GET  /v1/bridges/discover[?timeout_s=3]`` — browse ``_hil-bridge._tcp``,
  confirm each answer with ``GET /api/capabilities``, return proposals. Read-only.
* ``POST /v1/bridges/probe {"address"}`` — add-by-address: the same check, one proposal.
* ``POST /v1/bridges/apply {"address", "host_id"?, "token_env"?, "role"?,
  "capabilities"?}`` — create the host for a ``new`` proposal (re-checks first).

Discovery never adds, removes or disables hosts by itself; only ``apply`` writes,
and only one new host per call. The capabilities reads need only a bearer token
(like ``/v1/hosts``); discover/probe follow the inventory API (``inventory:read``,
all pools) and apply needs ``inventory:write``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from hil_controller import bridge_discovery
from hil_controller.adapters.bridge_capabilities import get_capabilities, load_cached
from hil_controller.api.twins import authorize
from hil_controller.auth.principal import Principal
from hil_controller.auth.tokens import require_auth

router = APIRouter(prefix="/v1/bridges", tags=["bridges"])
Auth = Annotated[Principal, Depends(require_auth)]


class ProbeRequest(BaseModel):
    address: str


class ApplyRequest(BaseModel):
    address: str
    host_id: str = ""
    token_env: str = ""
    role: str = ""
    capabilities: list[str] | None = None


def _client_for(request: Request, host_id: str) -> Any:
    from hil_controller.hosts.esp_bridge import bridge_client_of

    registry = getattr(request.app.state, "host_registry", None)
    if registry is None:
        return None
    try:
        return bridge_client_of(registry.transport_for(host_id))
    except (KeyError, ValueError):
        return None


@router.get("")
async def list_bridges(request: Request, _auth: Auth) -> dict[str, Any]:
    db_path: str = request.app.state.db_path
    out = []
    for host in await bridge_discovery.bridge_hosts(db_path):
        cached = await load_cached(db_path, host["id"])
        out.append(
            {
                "host_id": host["id"],
                "addr": host["addr"],
                "api_url": host["api_url"],
                "token_env": host["token_env"],
                "status": host["status"],
                "capabilities": cached.view(),
            }
        )
    return {"bridges": out}


@router.get("/discover")
async def discover_bridges(
    request: Request,
    principal: Auth,
    timeout_s: Annotated[float, Query(ge=0.5, le=15)] = bridge_discovery.DEFAULT_BROWSE_S,
) -> dict[str, Any]:
    authorize(principal)
    return await bridge_discovery.discover(request.app.state.db_path, timeout_s=timeout_s)


@router.post("/probe")
async def probe_bridge(request: Request, principal: Auth, body: ProbeRequest) -> dict[str, Any]:
    authorize(principal)
    if not body.address.strip():
        raise HTTPException(422, "address is required")
    proposal = await bridge_discovery.probe_address(request.app.state.db_path, body.address)
    return proposal.view()


@router.post("/apply", status_code=201)
async def apply_bridge(request: Request, principal: Auth, body: ApplyRequest) -> dict[str, Any]:
    authorize(principal, write=True)
    from hil_controller.hosts.registry import refresh_app_registry

    try:
        host = await bridge_discovery.apply_proposal(
            request.app.state.db_path,
            body.address,
            host_id=body.host_id,
            token_env=body.token_env,
            role=body.role,
            capabilities=body.capabilities,
        )
    except bridge_discovery.ApplyError as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    await refresh_app_registry(request.app)
    return {"created": host}


@router.get("/{host_id}/capabilities")
async def bridge_capabilities(
    request: Request, _auth: Auth, host_id: str, refresh: bool = False
) -> dict[str, Any]:
    db_path: str = request.app.state.db_path
    hosts = {h["id"]: h for h in await bridge_discovery.bridge_hosts(db_path)}
    if host_id not in hosts:
        raise HTTPException(404, f"{host_id!r} is not an esp-usbip-bridge host")
    cached = await get_capabilities(
        db_path, host_id, _client_for(request, host_id), refresh=refresh
    )
    return {"host_id": host_id, **cached.view()}
