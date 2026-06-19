"""POST /v1/jobs, GET /v1/jobs/{id}, /wait, /cancel."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import UTC
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from hil_controller.auth.principal import Principal
from hil_controller.auth.tokens import require_auth
from hil_controller.db.connection import (
    append_event,
    audit_event,
    get_db,
    get_events_since,
    get_job,
    insert_job,
    update_job_state,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/jobs", tags=["jobs"])

Auth = Annotated[Principal, Depends(require_auth)]

# Scripts that require the 'trusted-firmware' capability
_TRUSTED_SCRIPTS: frozenset[str] = frozenset({"raw-firmware-smoke", "firmware-bench"})


# --------------------------------------------------------------------------- #
# Request / response models                                                    #
# --------------------------------------------------------------------------- #


class DeviceSelector(BaseModel):
    kind: str | None = None
    model: str | None = None
    id: str | None = None
    capabilities: list[str] = Field(default_factory=list)


class AuxSelector(BaseModel):
    kind: str | None = None
    capabilities: list[str] = Field(default_factory=list)


class Target(BaseModel):
    device: DeviceSelector
    requires: list[AuxSelector] = Field(default_factory=list)
    pool: str = "public"


class PayloadSource(BaseModel):
    repo: str | None = None
    ref: str | None = None
    submodules: bool = False
    shallow: bool = True
    setup: list[str] = Field(default_factory=list)
    kind: str | None = None
    source: str | None = None
    tag: str | None = None
    asset: str | None = None
    sha256: str | None = None


class Payload(BaseModel):
    kind: str
    source: dict[str, Any] | None = None


class ExclusiveFlag(BaseModel):
    host: bool = False


class Timeouts(BaseModel):
    total_s: int = 1800
    flash_s: int = 120
    run_s: int = 300
    deploy_s: int = 300


class JobRequest(BaseModel):
    target: Target
    script: str
    params: dict[str, Any] = Field(default_factory=dict)
    payload: Payload | None = None
    secrets_profile: str = "bench-protomq"
    secrets: dict[str, str] = Field(default_factory=dict)
    exclusive: ExclusiveFlag = Field(default_factory=ExclusiveFlag)
    timeouts: Timeouts = Field(default_factory=Timeouts)
    metadata: dict[str, Any] = Field(default_factory=dict)


class JobSubmitResponse(BaseModel):
    id: str
    wait_url: str
    since: int = 0


class JobSnapshot(BaseModel):
    id: str
    state: str
    result: str | None
    assigned_host: str | None
    assigned_device: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    summary: str | None


class WaitResponse(BaseModel):
    events: list[dict[str, Any]]
    next_since: int
    state: str
    result: str | None = None


# --------------------------------------------------------------------------- #
# Routes                                                                       #
# --------------------------------------------------------------------------- #


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=JobSubmitResponse)
async def submit_job(request: Request, body: JobRequest, _auth: Auth) -> JobSubmitResponse:
    if not _auth.allows_pool(body.target.pool):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Pool '{body.target.pool}' not allowed for this token",
        )

    profile = body.secrets_profile or _auth.default_profile
    if not _auth.allows_profile(profile):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Profile '{profile}' not allowed for this token",
        )

    if body.script in _TRUSTED_SCRIPTS and not _auth.has_capability("trusted-firmware"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Script '{body.script}' requires trusted-firmware capability",
        )

    job_id = str(uuid.uuid4())
    db_path: str = request.app.state.db_path
    scheduler = request.app.state.scheduler

    async with get_db(db_path) as db:
        await insert_job(
            db,
            job_id=job_id,
            request_json=body.model_dump(),
            secrets_profile=profile,
            exclusive_host=body.exclusive.host,
            submitted_by=_auth.subject,
            repo=_auth.repo,
        )
        await append_event(db, job_id, "state", {"state": "queued"})
        await audit_event(
            db,
            "job.submit",
            subject=_auth.subject,
            repo=_auth.repo,
            entity_id=job_id,
            detail={"pool": body.target.pool, "script": body.script, "profile": profile},
        )

    base = str(request.base_url).rstrip("/")
    await scheduler.enqueue(job_id)

    return JobSubmitResponse(
        id=job_id,
        wait_url=f"{base}/v1/jobs/{job_id}/wait",
        since=0,
    )


@router.get("/{job_id}", response_model=JobSnapshot)
async def get_job_snapshot(request: Request, job_id: str, _auth: Auth) -> JobSnapshot:
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        row = await get_job(db, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobSnapshot(
        id=row["id"],
        state=row["state"],
        result=row["result"],
        assigned_host=row["assigned_host"],
        assigned_device=row["assigned_device"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        summary=row["summary"],
    )


class AssetInfo(BaseModel):
    id: str
    filename: str
    kind: str
    size_bytes: int
    created_at: str


class AssetList(BaseModel):
    assets: list[AssetInfo]


@router.get("/{job_id}/assets", response_model=AssetList)
async def list_job_assets(request: Request, job_id: str, _auth: Auth) -> AssetList:
    """List a job's captured assets (serial.log / protomq.log / flash.log, ...).

    Lets external CI pull proof for a run without scraping the web UI.
    """
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        if await get_job(db, job_id) is None:
            raise HTTPException(status_code=404, detail="Job not found")
        cur = await db.execute(
            "SELECT id, filename, kind, size_bytes, created_at FROM assets "
            "WHERE job_id = ? AND purged_at IS NULL ORDER BY created_at",
            (job_id,),
        )
        rows = await cur.fetchall()
    return AssetList(
        assets=[
            AssetInfo(
                id=r["id"],
                filename=r["filename"],
                kind=r["kind"],
                size_bytes=r["size_bytes"],
                created_at=r["created_at"],
            )
            for r in rows
        ]
    )


@router.get("/{job_id}/assets/{asset_id}/download")
async def download_job_asset(
    request: Request, job_id: str, asset_id: str, _auth: Auth
) -> FileResponse:
    """Stream a single asset file (e.g. serial.log) for a job."""
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        cur = await db.execute(
            "SELECT id, filename, path, kind FROM assets "
            "WHERE id = ? AND job_id = ? AND purged_at IS NULL",
            (asset_id, job_id),
        )
        r = await cur.fetchone()
    if r is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    path = r["path"]
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Asset file is not available on disk")
    media = "text/plain; charset=utf-8" if r["kind"] == "log" else "application/octet-stream"
    return FileResponse(path, filename=r["filename"], media_type=media)


@router.get("/{job_id}/wait", response_model=WaitResponse)
async def long_poll_wait(
    request: Request,
    job_id: str,
    _auth: Auth,
    since: int = Query(default=0, ge=0),
    timeout: int = Query(default=300, ge=1, le=600),
) -> WaitResponse:
    db_path: str = request.app.state.db_path
    event_bus = request.app.state.event_bus

    # First check job exists
    async with get_db(db_path) as db:
        row = await get_job(db, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")

    # Check for already-available events
    async with get_db(db_path) as db:
        events = await get_events_since(db, job_id, since)

    if not events:
        await event_bus.wait_for_events(job_id, timeout=float(timeout))
        async with get_db(db_path) as db:
            events = await get_events_since(db, job_id, since)
            row = await get_job(db, job_id)

    next_since = events[-1]["seq"] if events else since
    return WaitResponse(
        events=events,
        next_since=next_since,
        state=row["state"],  # type: ignore[index]
        result=row["result"],  # type: ignore[index]
    )


class ExtendRequest(BaseModel):
    minutes: int = Field(default=30, ge=1, le=600)


@router.post("/{job_id}/extend", status_code=status.HTTP_202_ACCEPTED)
async def extend_job(
    request: Request, job_id: str, body: ExtendRequest, _auth: Auth
) -> dict[str, Any]:
    """Extend an interactive hold's window by pushing its lease ``expires_at``.

    The new expiry is ``max(now, current expiry) + minutes`` so repeated
    extends accumulate rather than reset. Returns the new expiry.
    """
    from datetime import datetime, timedelta, timezone

    from hil_controller.queue.leases import get_active_for_job, renew

    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        row = await get_job(db, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if row["state"] in ("finished", "error", "timeout", "cancelled"):
        raise HTTPException(status_code=409, detail=f"Job in terminal state: {row['state']}")

    lease = await get_active_for_job(db_path, job_id)
    if lease is None:
        raise HTTPException(status_code=409, detail="Job holds no active lease/window to extend")

    now = datetime.now(UTC)
    base = now
    if lease.get("expires_at"):
        try:
            current = datetime.fromisoformat(lease["expires_at"])
            if current > now:
                base = current
        except ValueError:
            pass
    new_expiry = (base + timedelta(minutes=body.minutes)).isoformat()
    if not await renew(db_path, lease["id"], expires_at=new_expiry):
        raise HTTPException(status_code=409, detail="Lease no longer active")

    async with get_db(db_path) as db:
        await append_event(
            db, job_id, "window", {"expires_at": new_expiry, "extended_minutes": body.minutes}
        )
        await audit_event(
            db,
            "job.extend",
            subject=_auth.subject,
            repo=_auth.repo,
            entity_id=job_id,
            detail={"minutes": body.minutes, "expires_at": new_expiry},
        )
    event_bus = request.app.state.event_bus
    await event_bus.publish(job_id, {"kind": "window", "payload": {"expires_at": new_expiry}})

    return {"status": "extended", "id": job_id, "expires_at": new_expiry}


@router.post("/{job_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_job(request: Request, job_id: str, _auth: Auth) -> dict[str, str]:
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        row = await get_job(db, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")

    if row["state"] in ("finished", "error", "timeout", "cancelled"):
        raise HTTPException(
            status_code=409, detail=f"Job already in terminal state: {row['state']}"
        )

    async with get_db(db_path) as db:
        await update_job_state(db, job_id, "cancelled", result="cancelled")
        await append_event(db, job_id, "state", {"state": "cancelled"})
        await audit_event(
            db,
            "job.cancel",
            subject=_auth.subject,
            repo=_auth.repo,
            entity_id=job_id,
        )

    # Actually stop the running worker (not just mark the DB) — interactive
    # holds have no total timeout and won't notice a DB-only cancel.
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        scheduler.cancel_job(job_id)

    event_bus = request.app.state.event_bus
    await event_bus.publish(job_id, {"kind": "state", "payload": {"state": "cancelled"}})

    return {"status": "cancelled", "id": job_id}
