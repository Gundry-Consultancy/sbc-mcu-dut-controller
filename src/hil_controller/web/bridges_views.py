"""Web UI: esp-usbip-bridge discovery → proposals → apply (``/ui/twins/bridges``).

The page mirrors ``/v1/bridges/*``: **Discover** browses mDNS
``_hil-bridge._tcp`` and confirms each answer with ``GET /api/capabilities``;
**Add by address** runs the same check for one address (works over Tailscale,
where multicast does not reach). Both only *show* proposals. **Apply** creates
one host for one ``new`` proposal after re-running the check; nothing is ever
added, removed or disabled automatically. Same access policy as the twins pages
(inventory capability + all pools; apply needs ``inventory:write``).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Form, Request
from fastapi.responses import RedirectResponse, Response

from hil_controller import bridge_discovery
from hil_controller.web.router import _tr
from hil_controller.web.twins_views import _guard, _missing

router = APIRouter(prefix="/ui", include_in_schema=False)


def _page(
    request: Request,
    hil_token: str,
    principal: Any,
    *,
    discovery: dict[str, Any] | None = None,
    proposals: list[dict[str, Any]] | None = None,
    error: str = "",
    notice: str = "",
    address: str = "",
    status_code: int = 200,
) -> Response:
    resp: Response = _tr(
        request,
        "bridges.html",
        {
            "token": hil_token,
            "active": "twins",
            "title": "Bridges — discovery",
            "discovery": discovery,
            "proposals": proposals if proposals is not None else (
                (discovery or {}).get("proposals") or []
            ),
            "error": error,
            "notice": notice,
            "address": address,
            "zeroconf": bridge_discovery.zeroconf_available(),
            "can_write": bool(principal) and not _missing(principal, write=True),
        },
        status_code=status_code,
    )
    return resp


@router.get("/twins/bridges")
async def bridges_page(
    request: Request, hil_token: str = Cookie(default=""), notice: str = ""
) -> Response:
    principal, denied = await _guard(request, hil_token)
    if denied is not None:
        return denied
    return _page(request, hil_token, principal, notice=notice)


@router.post("/twins/bridges/discover")
async def bridges_discover(
    request: Request,
    hil_token: str = Cookie(default=""),
    timeout_s: Annotated[str, Form()] = "3",
) -> Response:
    principal, denied = await _guard(request, hil_token)
    if denied is not None:
        return denied
    try:
        timeout = min(max(float(timeout_s or 3), 0.5), 15.0)
    except ValueError:
        timeout = bridge_discovery.DEFAULT_BROWSE_S
    result = await bridge_discovery.discover(request.app.state.db_path, timeout_s=timeout)
    return _page(request, hil_token, principal, discovery=result)


@router.post("/twins/bridges/probe")
async def bridges_probe(
    request: Request,
    hil_token: str = Cookie(default=""),
    address: Annotated[str, Form()] = "",
) -> Response:
    principal, denied = await _guard(request, hil_token)
    if denied is not None:
        return denied
    if not address.strip():
        return _page(request, hil_token, principal, error="Enter an address", status_code=422)
    proposal = await bridge_discovery.probe_address(request.app.state.db_path, address)
    return _page(request, hil_token, principal, proposals=[proposal.view()], address=address)


@router.post("/twins/bridges/apply")
async def bridges_apply(
    request: Request,
    hil_token: str = Cookie(default=""),
    address: Annotated[str, Form()] = "",
    host_id: Annotated[str, Form()] = "",
    token_env: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "",
    capabilities: Annotated[str, Form()] = "",
) -> Response:
    principal, denied = await _guard(request, hil_token, write=True)
    if denied is not None:
        return denied
    from hil_controller.hosts.registry import refresh_app_registry

    caps = capabilities.replace(",", " ").split() if capabilities.strip() else None
    try:
        host = await bridge_discovery.apply_proposal(
            request.app.state.db_path,
            address,
            host_id=host_id,
            token_env=token_env,
            role=role,
            capabilities=caps,
        )
    except bridge_discovery.ApplyError as exc:
        proposal = await bridge_discovery.probe_address(request.app.state.db_path, address)
        return _page(
            request,
            hil_token,
            principal,
            proposals=[proposal.view()],
            error=exc.detail,
            address=address,
            status_code=exc.status,
        )
    await refresh_app_registry(request.app)
    return RedirectResponse(f"/ui/hosts/{host['id']}", status_code=303)
