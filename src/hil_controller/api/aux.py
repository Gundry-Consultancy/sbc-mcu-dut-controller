"""GET /v1/aux, GET /v1/aux/{id}"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from hil_controller.auth.principal import Principal
from hil_controller.auth.tokens import require_auth
from hil_controller.db.connection import get_db

router = APIRouter(prefix="/v1/aux", tags=["aux"])
Auth = Annotated[Principal, Depends(require_auth)]


class AuxSummary(BaseModel):
    id: str
    kind: str
    model: str
    capabilities: list[str]
    interface: str
    observability: str
    pool: str
    status: str


class AuxDetail(AuxSummary):
    connections: list[dict[str, Any]]


@router.get("", response_model=list[AuxSummary])
async def list_auxes(
    request: Request,
    _auth: Auth,
    kind: str | None = Query(default=None),
    capability: str | None = Query(default=None),
    pool: str | None = Query(default=None),
) -> list[AuxSummary]:
    db_path: str = request.app.state.db_path
    filters: list[str] = []
    params: list[Any] = []

    if kind:
        filters.append("a.kind = ?")
        params.append(kind)
    if pool:
        filters.append("a.pool = ?")
        params.append(pool)

    capability_join = ""
    if capability:
        capability_join = ", json_each(a.capabilities_json) AS jcap"
        filters.append("jcap.value = ?")
        params.append(capability)

    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    sql = f"SELECT DISTINCT a.* FROM auxes a{capability_join} {where} ORDER BY a.id"

    async with get_db(db_path) as db:
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()

    return [
        AuxSummary(
            id=r["id"],
            kind=r["kind"],
            model=r["model"],
            capabilities=json.loads(r["capabilities_json"]),
            interface=r["interface"],
            observability=r["observability"],
            pool=r["pool"],
            status=r["status"],
        )
        for r in rows
    ]


@router.get("/{aux_id}", response_model=AuxDetail)
async def get_aux(request: Request, aux_id: str, _auth: Auth) -> AuxDetail:
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        async with db.execute("SELECT * FROM auxes WHERE id = ?", (aux_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Aux not found")

        async with db.execute("SELECT * FROM connections WHERE aux_id = ?", (aux_id,)) as cur:
            conn_rows = await cur.fetchall()

    return AuxDetail(
        id=row["id"],
        kind=row["kind"],
        model=row["model"],
        capabilities=json.loads(row["capabilities_json"]),
        interface=row["interface"],
        observability=row["observability"],
        pool=row["pool"],
        status=row["status"],
        connections=[dict(c) for c in conn_rows],
    )
