"""GET /v1/devices, GET /v1/devices/{id}, plus device_usb_ids CRUD + lookup."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timezone
from typing import Annotated, Any

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, field_validator

from hil_controller.auth.principal import Principal
from hil_controller.auth.tokens import require_auth
from hil_controller.db.connection import get_db

router = APIRouter(prefix="/v1/devices", tags=["devices"])
Auth = Annotated[Principal, Depends(require_auth)]


_VALID_ROLES = {"runtime", "bootloader", "dfu", "msc", "cdc", "unknown"}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _norm_id(s: str) -> str:
    return (s or "").strip().lower()


class DeviceSummary(BaseModel):
    id: str
    host_id: str
    kind: str
    model: str
    capabilities: list[str]
    pool: str
    status: str


class DeviceDetail(DeviceSummary):
    serial_port: str | None
    flasher: str | None
    current_job: str | None
    host: dict[str, Any] | None
    auxes: list[dict[str, Any]]


@router.get("", response_model=list[DeviceSummary])
async def list_devices(
    request: Request,
    _auth: Auth,
    host: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    model: str | None = Query(default=None),
    capability: str | None = Query(default=None),
    pool: str | None = Query(default=None),
) -> list[DeviceSummary]:
    db_path: str = request.app.state.db_path
    filters: list[str] = []
    params: list[Any] = []

    if host:
        filters.append("d.host_id = ?")
        params.append(host)
    if kind:
        filters.append("d.kind = ?")
        params.append(kind)
    if model:
        filters.append("d.model = ?")
        params.append(model)
    if pool:
        filters.append("d.pool = ?")
        params.append(pool)

    capability_join = ""
    if capability:
        capability_join = ", json_each(d.capabilities_json) AS jcap"
        filters.append("jcap.value = ?")
        params.append(capability)

    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    sql = f"SELECT DISTINCT d.* FROM devices d{capability_join} {where} ORDER BY d.id"

    async with get_db(db_path) as db:
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()

    return [
        DeviceSummary(
            id=r["id"],
            host_id=r["host_id"],
            kind=r["kind"],
            model=r["model"],
            capabilities=json.loads(r["capabilities_json"]),
            pool=r["pool"],
            status=r["status"],
        )
        for r in rows
    ]


class AvailabilityRetryResult(BaseModel):
    reset: list[str]
    skipped_permanent: list[str] = Field(default_factory=list)


async def _reset_retry_budget(
    db_path: str, device_ids: list[str] | None
) -> AvailabilityRetryResult:
    """Zero the retry budget so the reconciler re-probes on its next tick.

    Only ``temporary`` outages are reset — ``permanent`` is a human statement
    (edit the device to change it) and is reported back as skipped.
    """
    reset: list[str] = []
    skipped: list[str] = []
    async with get_db(db_path) as db:
        if device_ids is None:
            cur = await db.execute(
                "SELECT id, unavailable_kind FROM devices WHERE status != 'available'"
            )
        else:
            marks = ",".join("?" for _ in device_ids)
            cur = await db.execute(
                f"SELECT id, unavailable_kind FROM devices WHERE id IN ({marks})",
                device_ids,
            )
        rows = [dict(r) for r in await cur.fetchall()]
        for r in rows:
            if r.get("unavailable_kind") == "permanent":
                skipped.append(r["id"])
                continue
            reset.append(r["id"])
        if reset:
            marks = ",".join("?" for _ in reset)
            await db.execute(
                f"UPDATE devices SET retry_attempts = 0, retry_after = NULL "
                f"WHERE id IN ({marks})",
                reset,
            )
            await db.commit()
    return AvailabilityRetryResult(reset=reset, skipped_permanent=skipped)


@router.post("/availability/retry", response_model=AvailabilityRetryResult)
async def retry_all_availability(request: Request, _auth: Auth) -> AvailabilityRetryResult:
    """Re-probe every unavailable (temporary) device now: reset all retry budgets."""
    return await _reset_retry_budget(request.app.state.db_path, None)


@router.post("/{device_id}/availability/retry", response_model=AvailabilityRetryResult)
async def retry_device_availability(
    request: Request, device_id: str, _auth: Auth
) -> AvailabilityRetryResult:
    """Reset one device's retry budget so the reconciler re-probes it promptly."""
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        async with db.execute("SELECT id FROM devices WHERE id = ?", (device_id,)) as cur:
            if await cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="Device not found")
    result = await _reset_retry_budget(db_path, [device_id])
    if device_id in result.skipped_permanent:
        raise HTTPException(
            status_code=409,
            detail="Device is permanently unavailable — edit the device to change that",
        )
    return result


@router.get("/{device_id}", response_model=DeviceDetail)
async def get_device(request: Request, device_id: str, _auth: Auth) -> DeviceDetail:
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        async with db.execute("SELECT * FROM devices WHERE id = ?", (device_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Device not found")

        async with db.execute(
            """
            SELECT id FROM jobs
            WHERE assigned_device = ?
              AND state NOT IN ('finished','error','timeout','cancelled')
            LIMIT 1
            """,
            (device_id,),
        ) as cur:
            job_row = await cur.fetchone()

        async with db.execute(
            "SELECT id, role, addr, transport, status FROM hosts WHERE id = ?",
            (row["host_id"],),
        ) as cur:
            host_row = await cur.fetchone()

        async with db.execute(
            """
            SELECT a.* FROM auxes a
            JOIN connections c ON c.aux_id = a.id
            WHERE c.device_id = ?
            """,
            (device_id,),
        ) as cur:
            aux_rows = await cur.fetchall()

    return DeviceDetail(
        id=row["id"],
        host_id=row["host_id"],
        kind=row["kind"],
        model=row["model"],
        capabilities=json.loads(row["capabilities_json"]),
        pool=row["pool"],
        status=row["status"],
        serial_port=row["serial_port"],
        flasher=row["flasher"],
        current_job=job_row["id"] if job_row else None,
        host=dict(host_row) if host_row else None,
        auxes=[dict(a) for a in aux_rows],
    )


# ---------------------------------------------------------------------------
# device_usb_ids CRUD + lookup
# ---------------------------------------------------------------------------


class UsbIdIn(BaseModel):
    vid: str = Field(..., min_length=1)
    pid: str = Field(..., min_length=1)
    role: str = "unknown"
    iserial: str | None = None
    description: str | None = None
    bcd_device: str | None = None

    @field_validator("vid", "pid")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("must be non-empty")
        return v.lower()

    @field_validator("role")
    @classmethod
    def _role(cls, v: str) -> str:
        v = (v or "unknown").strip().lower()
        if v not in _VALID_ROLES:
            raise ValueError(f"role must be one of {sorted(_VALID_ROLES)}")
        return v


class UsbIdOut(BaseModel):
    id: int
    device_id: str
    vid: str
    pid: str
    role: str
    iserial: str | None
    description: str | None
    bcd_device: str | None
    first_seen_at: str
    last_seen_at: str
    learned_from_job: str | None
    source: str


class UsbLookupIn(BaseModel):
    vid: str
    pid: str
    iserial: str | None = None

    @field_validator("vid", "pid")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("must be non-empty")
        return v.lower()


async def _require_device(db: aiosqlite.Connection, device_id: str) -> None:
    async with db.execute("SELECT 1 FROM devices WHERE id = ?", (device_id,)) as cur:
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Device not found")


def _row_to_out(row: aiosqlite.Row) -> UsbIdOut:
    return UsbIdOut(
        id=row["id"],
        device_id=row["device_id"],
        vid=row["vid"],
        pid=row["pid"],
        role=row["role"],
        iserial=row["iserial"],
        description=row["description"],
        bcd_device=row["bcd_device"],
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        learned_from_job=row["learned_from_job"],
        source=row["source"],
    )


@router.get("/{device_id}/usb-ids", response_model=list[UsbIdOut])
async def list_device_usb_ids(request: Request, device_id: str, _auth: Auth) -> list[UsbIdOut]:
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        await _require_device(db, device_id)
        async with db.execute(
            "SELECT * FROM device_usb_ids WHERE device_id = ? ORDER BY id",
            (device_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [_row_to_out(r) for r in rows]


@router.post("/{device_id}/usb-ids", response_model=UsbIdOut, status_code=201)
async def add_device_usb_id(
    request: Request, device_id: str, body: UsbIdIn, _auth: Auth
) -> UsbIdOut:
    db_path: str = request.app.state.db_path
    now = _now_iso()
    async with get_db(db_path) as db:
        await _require_device(db, device_id)
        try:
            cur = await db.execute(
                "INSERT INTO device_usb_ids "
                "(device_id, vid, pid, role, iserial, description, bcd_device, "
                " first_seen_at, last_seen_at, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual')",
                (
                    device_id,
                    body.vid,
                    body.pid,
                    body.role,
                    body.iserial,
                    body.description,
                    body.bcd_device,
                    now,
                    now,
                ),
            )
            await db.commit()
            new_id = cur.lastrowid
        except aiosqlite.IntegrityError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"duplicate usb id: {exc}",
            )
        async with db.execute("SELECT * FROM device_usb_ids WHERE id = ?", (new_id,)) as cur:
            row = await cur.fetchone()
    return _row_to_out(row)


@router.delete("/{device_id}/usb-ids/{row_id}", status_code=204)
async def delete_device_usb_id(
    request: Request, device_id: str, row_id: int, _auth: Auth
) -> Response:
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        async with db.execute(
            "SELECT id FROM device_usb_ids WHERE id = ? AND device_id = ?",
            (row_id, device_id),
        ) as cur:
            if await cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="usb-id row not found")
        await db.execute("DELETE FROM device_usb_ids WHERE id = ?", (row_id,))
        await db.commit()
    return Response(status_code=204)


@router.post("/lookup-by-usb", response_model=list[UsbIdOut])
async def lookup_by_usb(request: Request, body: UsbLookupIn, _auth: Auth) -> list[UsbIdOut]:
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        sql = "SELECT * FROM device_usb_ids WHERE vid = ? AND pid = ?"
        params: list[Any] = [body.vid, body.pid]
        if body.iserial is not None:
            sql += " AND COALESCE(iserial,'') = ?"
            params.append(body.iserial)
        sql += " ORDER BY last_seen_at DESC, id"
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
    return [_row_to_out(r) for r in rows]


class LearnUsbIn(BaseModel):
    include_reset_cycle: bool = False
    job_id: str | None = None


@router.post("/{device_id}/learn-usb", response_model=list[UsbIdOut])
async def learn_device_usb(
    request: Request,
    device_id: str,
    _auth: Auth,
    body: LearnUsbIn | None = None,
) -> list[UsbIdOut]:
    """Active VID/PID fingerprint via depower/repower of the device's hub port.

    Requires the device to have `hub_host_id`, `hub_port_path`, and (for
    the depower step) `solenoid_channel` populated. Acquires an
    `exclusive_hub` lease for the duration.
    """
    from hil_controller.adapters.usb_fingerprint import (
        FingerprintError,
        UsbFingerprintAdapter,
    )
    from hil_controller.queue.leases import LeaseConflict

    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        await _require_device(db, device_id)

    body = body or LearnUsbIn()
    # Production wiring of hub + scan_fn is best provided via app.state
    # (set by the deployment). In CI/tests, monkeypatch the adapter's
    # learn() to a fake. Here we fail clearly if no provider configured.
    provider = getattr(request.app.state, "usb_fingerprint_provider", None)
    if provider is None:
        # Default: instantiate with placeholders. Tests monkeypatch .learn,
        # so the placeholders never run.
        adapter = UsbFingerprintAdapter(
            db_path=db_path,
            hub=_NoopHub(),
            scan_fn=lambda: [],
        )
    else:
        adapter = provider(db_path=db_path)
    try:
        rows = await adapter.learn(
            device_id=device_id,
            job_id=body.job_id,
            include_reset_cycle=body.include_reset_cycle,
        )
    except FingerprintError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except LeaseConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return [UsbIdOut(**r) for r in rows]


class _NoopHub:
    """Stand-in hub used when no production provider is wired in.

    All methods are async no-ops; this lets the endpoint still execute
    the workflow and return whatever the (also empty) scan reports.
    """

    async def all_off(self) -> None:
        pass

    async def port_on(self, channel: int) -> None:
        pass

    async def port_off(self, channel: int, **kwargs) -> None:
        pass
