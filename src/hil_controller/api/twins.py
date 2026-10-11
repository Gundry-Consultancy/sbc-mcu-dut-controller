"""Administrative physical inventory API; discovery never changes scheduling implicitly."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from hil_controller import twins
from hil_controller.auth.principal import Principal
from hil_controller.auth.tokens import require_auth
from hil_controller.db.connection import get_db
from hil_controller.twin_models import CloseConnection, Connection, Observation, SlotKind

router = APIRouter(prefix="/v1/twins", tags=["physical inventory"])
Auth = Annotated[Principal, Depends(require_auth)]


def authorize(principal: Principal, *, write: bool = False) -> None:
    capability = "inventory:write" if write else "inventory:read"
    permitted = principal.has_capability(capability)
    if not write:
        permitted = permitted or principal.has_capability("inventory:write")
    # Physical infrastructure crosses scheduling pools, and legacy peripheral and
    # host rows have no pool. Do not leak cross-pool serials through graph traversal.
    if not permitted or not principal.allows_pool("*"):
        raise HTTPException(403, "Physical inventory requires inventory capability and all pools")


def error(exc: twins.TwinError) -> HTTPException:
    return HTTPException(exc.status, exc.detail)


@router.get("")
async def list_twins(
    request: Request,
    principal: Auth,
    kind: str | None = None,
    pool: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    authorize(principal)
    async with get_db(request.app.state.db_path) as db:
        items = await twins.rows(
            db,
            "SELECT t.*,COALESCE((SELECT json_extract(o.payload_json,'$.label') "
            "FROM twin_observations o WHERE o.twin_id=t.id "
            "AND json_extract(o.payload_json,'$.label') IS NOT NULL "
            "ORDER BY o.observed_at DESC,o.observation_id DESC LIMIT 1),t.label) AS current_label "
            "FROM physical_twins t WHERE (? IS NULL OR kind=?) "
            "AND (? IS NULL OR pool=?) ORDER BY id LIMIT ? OFFSET ?",
            (kind, kind, pool, pool, limit, offset),
        )
    for item in items:
        item["label"] = item.pop("current_label")
    return {"twins": items, "limit": limit, "offset": offset}


@router.get("/slots")
async def list_slots(request: Request, principal: Auth, kind: SlotKind) -> dict[str, Any]:
    authorize(principal)
    table = twins.SLOT_TABLES[kind]
    async with get_db(request.app.state.db_path) as db:
        slots = await twins.rows(
            db,
            f"SELECT s.id,a.twin_id,a.assigned_at FROM {table} s "
            "LEFT JOIN twin_slot_assignments a ON a.slot_id=s.id "
            "AND a.slot_kind=? AND a.ended_at IS NULL ORDER BY s.id",
            (kind,),
        )
        history = await twins.rows(
            db,
            "SELECT * FROM twin_slot_assignments WHERE slot_kind=? ORDER BY assigned_at,id",
            (kind,),
        )
    return {"kind": kind, "slots": slots, "history": history}


@router.post("/observations/preview")
async def preview(request: Request, principal: Auth, observation: Observation) -> dict[str, Any]:
    authorize(principal)
    async with get_db(request.app.state.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            matches = await twins.candidates(db, observation)
            try:
                result = await twins.ingest(db, observation, principal.subject)
                # Generated twin IDs are provisional and deliberately not returned.
                return {
                    "valid": True,
                    "candidates": matches,
                    "action": "update" if observation.twin_id else "create",
                    "replayed": result["replayed"],
                }
            except twins.TwinError as exc:
                return {
                    "valid": False,
                    "candidates": matches,
                    "conflict": {"status": exc.status, "detail": exc.detail},
                }
        finally:
            await db.rollback()


@router.post("/observations")
async def observe(request: Request, principal: Auth, observation: Observation) -> dict[str, Any]:
    authorize(principal, write=True)
    async with get_db(request.app.state.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            result = await twins.ingest(db, observation, principal.subject)
            await db.commit()
            return result
        except twins.TwinError as exc:
            await db.rollback()
            raise error(exc) from exc


@router.post("/connections")
async def create_connection(request: Request, principal: Auth, item: Connection) -> dict[str, Any]:
    authorize(principal, write=True)
    async with get_db(request.app.state.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            result = await twins.connect(db, item, principal.subject)
            await db.commit()
            return result
        except twins.TwinError as exc:
            await db.rollback()
            raise error(exc) from exc


@router.post("/connections/{connection_id}/close")
async def close_connection(
    connection_id: str,
    request: Request,
    principal: Auth,
    item: CloseConnection,
) -> dict[str, Any]:
    authorize(principal, write=True)
    async with get_db(request.app.state.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        existing = await twins.rows(
            db, "SELECT * FROM twin_connections WHERE id=?", (connection_id,)
        )
        if not existing:
            raise HTTPException(404, "Connection not found")
        connection = existing[0]
        at = twins.iso(item.ended_at)
        if at < connection["started_at"]:
            raise HTTPException(422, "Connection cannot end before it starts")
        if connection["ended_at"] is not None and (
            connection["ended_at"] != at
            or connection["close_source"] != item.source
            or connection["close_reason"] != item.reason
        ):
            raise HTTPException(409, "Connection is already closed with different evidence")
        await db.execute(
            "UPDATE twin_connections SET ended_at=?,close_source=?,close_reason=?,close_actor=? "
            "WHERE id=?",
            (at, item.source, item.reason, principal.subject, connection_id),
        )
        await db.commit()
        return {"id": connection_id, "ended_at": at}


@router.get("/{twin_id}")
async def get_twin(twin_id: str, request: Request, principal: Auth) -> dict[str, Any]:
    authorize(principal)
    async with get_db(request.app.state.db_path) as db:
        try:
            return await twins.detail(db, twin_id)
        except twins.TwinError as exc:
            raise error(exc) from exc
