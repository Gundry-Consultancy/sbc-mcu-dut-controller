"""POST / DELETE / GET /v1/leases — device/hub exclusivity leases."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from hil_controller.auth.principal import Principal
from hil_controller.auth.tokens import require_auth
from hil_controller.queue.leases import (
    VALID_KINDS,
    LeaseConflict,
    acquire,
    list_active,
    list_all,
    release,
)

router = APIRouter(prefix="/v1/leases", tags=["leases"])
Auth = Annotated[Principal, Depends(require_auth)]


class LeaseIn(BaseModel):
    kind: str
    device_id: str | None = None
    hub_host_id: str | None = None
    job_id: str | None = None
    expires_at: str | None = None


class LeaseOut(BaseModel):
    id: int
    device_id: str | None
    hub_host_id: str | None
    job_id: str | None
    kind: str
    acquired_at: str
    expires_at: str | None
    released_at: str | None


@router.get("", response_model=list[LeaseOut])
async def list_leases(request: Request, _auth: Auth, active_only: bool = True) -> list[LeaseOut]:
    db_path: str = request.app.state.db_path
    rows = await (list_active(db_path) if active_only else list_all(db_path))
    return [LeaseOut(**r) for r in rows]


@router.post("", response_model=LeaseOut, status_code=201)
async def create_lease(request: Request, body: LeaseIn, _auth: Auth) -> LeaseOut:
    if body.kind not in VALID_KINDS:
        raise HTTPException(
            status_code=422,
            detail=f"kind must be one of {sorted(VALID_KINDS)}",
        )
    db_path: str = request.app.state.db_path
    try:
        row = await acquire(
            db_path,
            kind=body.kind,
            device_id=body.device_id,
            hub_host_id=body.hub_host_id,
            job_id=body.job_id,
            expires_at=body.expires_at,
        )
    except LeaseConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return LeaseOut(**row)


@router.delete("/{lease_id}", status_code=204)
async def delete_lease(request: Request, lease_id: int, _auth: Auth) -> Response:
    db_path: str = request.app.state.db_path
    if not await release(db_path, lease_id):
        raise HTTPException(status_code=404, detail="lease not found or already released")
    return Response(status_code=204)
