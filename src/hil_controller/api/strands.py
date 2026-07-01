"""CRUD for I2C component strands — the DB is the source of truth.

A *strand* is a shared I2C component chain the analog strand-mux routes to one
DUT at a time. Strands, their components, and their per-DUT analog-mux routes are
edited here (and via the web form); ``GET /v1/topology/export`` backports the
live DB into reseedable topology YAML.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from hil_controller.auth.principal import Principal
from hil_controller.auth.tokens import require_auth
from hil_controller.db.connection import get_db

router = APIRouter(prefix="/v1/strands", tags=["strands"])
Auth = Annotated[Principal, Depends(require_auth)]


class StrandComponent(BaseModel):
    id: str
    model: str = ""
    address: int | None = None
    tca_channel: int | None = None  # None = direct bus; int = on-strand TCA channel
    ws_types: list[int] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    notes: str | None = None


class StrandRoute(BaseModel):
    device: str
    channel: int


class Strand(BaseModel):
    id: str
    mux_aux: str | None = None  # auxes.id of the analog strand-mux
    mux_group: str | None = None
    tca_address: int | None = None
    pool: str = "public"
    status: str = "available"
    notes: str | None = None
    components: list[StrandComponent] = Field(default_factory=list)
    routes: list[StrandRoute] = Field(default_factory=list)


async def _load_strand(db: aiosqlite.Connection, strand_id: str) -> dict[str, Any] | None:
    async with db.execute("SELECT * FROM strands WHERE id = ?", (strand_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    s = dict(row)
    out: dict[str, Any] = {
        "id": s["id"],
        "mux_aux": s["mux_aux_id"],
        "mux_group": s["mux_group"],
        "tca_address": s["tca_address"],
        "pool": s["pool"],
        "status": s["status"],
        "notes": s["notes"],
        "components": [],
        "routes": [],
    }
    async with db.execute(
        "SELECT * FROM strand_components WHERE strand_id = ? ORDER BY id", (strand_id,)
    ) as cur:
        for row in await cur.fetchall():
            c = dict(row)
            out["components"].append(
                {
                    "id": c["id"],
                    "model": c["model"],
                    "address": c["address"],
                    "tca_channel": c["tca_channel"],
                    "ws_types": json.loads(c["ws_types_json"] or "[]"),
                    "capabilities": json.loads(c["capabilities_json"] or "[]"),
                    "notes": c["notes"],
                }
            )
    async with db.execute(
        "SELECT device_id, mux_channel FROM device_strands "
        "WHERE strand_id = ? ORDER BY mux_channel",
        (strand_id,),
    ) as cur:
        for r in await cur.fetchall():
            out["routes"].append({"device": r["device_id"], "channel": r["mux_channel"]})
    return out


async def _write_strand(db: aiosqlite.Connection, s: Strand) -> None:
    """Upsert a strand and REPLACE its components + routes (declarative)."""
    await db.execute(
        """
        INSERT INTO strands (id, mux_aux_id, mux_group, tca_address, pool, status, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            mux_aux_id=excluded.mux_aux_id, mux_group=excluded.mux_group,
            tca_address=excluded.tca_address, pool=excluded.pool,
            status=excluded.status, notes=excluded.notes
        """,
        (s.id, s.mux_aux, s.mux_group, s.tca_address, s.pool, s.status, s.notes),
    )
    await db.execute("DELETE FROM strand_components WHERE strand_id = ?", (s.id,))
    for c in s.components:
        await db.execute(
            """
            INSERT INTO strand_components
                (id, strand_id, model, address, tca_channel, ws_types_json,
                 capabilities_json, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                c.id,
                s.id,
                c.model,
                c.address,
                c.tca_channel,
                json.dumps(c.ws_types),
                json.dumps(c.capabilities),
                c.notes,
            ),
        )
    await db.execute("DELETE FROM device_strands WHERE strand_id = ?", (s.id,))
    for r in s.routes:
        await db.execute(
            "INSERT INTO device_strands (device_id, strand_id, mux_channel) VALUES (?, ?, ?)",
            (r.device, s.id, r.channel),
        )
    await db.commit()


@router.get("")
async def list_strands(request: Request, _auth: Auth) -> dict[str, Any]:
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        async with db.execute("SELECT id FROM strands ORDER BY id") as cur:
            ids = [r["id"] for r in await cur.fetchall()]
        strands = [await _load_strand(db, sid) for sid in ids]
    return {"strands": strands}


@router.get("/{strand_id}")
async def get_strand(request: Request, strand_id: str, _auth: Auth) -> dict[str, Any]:
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        s = await _load_strand(db, strand_id)
    if s is None:
        raise HTTPException(status_code=404, detail=f"unknown strand {strand_id!r}")
    return s


@router.post("", status_code=201)
async def create_strand(request: Request, body: Strand, _auth: Auth) -> dict[str, Any]:
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        if await _load_strand(db, body.id) is not None:
            raise HTTPException(status_code=409, detail=f"strand {body.id!r} already exists")
        await _write_strand(db, body)
        return await _load_strand(db, body.id)


@router.put("/{strand_id}")
async def upsert_strand(
    request: Request, strand_id: str, body: Strand, _auth: Auth
) -> dict[str, Any]:
    if body.id != strand_id:
        raise HTTPException(status_code=400, detail="body id must match path id")
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        await _write_strand(db, body)
        return await _load_strand(db, strand_id)


@router.delete("/{strand_id}", status_code=204)
async def delete_strand(request: Request, strand_id: str, _auth: Auth) -> None:
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        cur = await db.execute("DELETE FROM strands WHERE id = ?", (strand_id,))
        await db.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"unknown strand {strand_id!r}")
