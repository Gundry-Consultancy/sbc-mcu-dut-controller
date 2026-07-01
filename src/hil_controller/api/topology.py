"""GET /v1/topology, POST /v1/topology/resolve"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from hil_controller.auth.principal import Principal
from hil_controller.auth.tokens import require_auth
from hil_controller.db.connection import get_db

router = APIRouter(prefix="/v1/topology", tags=["topology"])
Auth = Annotated[Principal, Depends(require_auth)]


class DeviceSelector(BaseModel):
    id: str | None = None
    kind: str | None = None
    model: str | None = None
    capabilities: list[str] = []


class AuxSelector(BaseModel):
    kind: str | None = None
    capabilities: list[str] = []


class ResolveTarget(BaseModel):
    device: DeviceSelector
    requires: list[AuxSelector] = []
    pool: str = "public"


class Candidate(BaseModel):
    host_id: str
    device_id: str
    aux_bindings: list[dict[str, Any]]
    mux_ops: list[dict[str, Any]]


class ResolveResponse(BaseModel):
    candidates: list[Candidate]
    rejected: list[dict[str, Any]]


@router.get("")
async def get_topology(request: Request, _auth: Auth) -> dict[str, Any]:
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        async with db.execute("SELECT * FROM hosts ORDER BY id") as cur:
            host_rows = await cur.fetchall()
        async with db.execute("SELECT * FROM devices ORDER BY id") as cur:
            device_rows = await cur.fetchall()
        async with db.execute("SELECT * FROM auxes ORDER BY id") as cur:
            aux_rows = await cur.fetchall()
        async with db.execute("SELECT * FROM connections") as cur:
            conn_rows = await cur.fetchall()
        async with db.execute("SELECT * FROM peripherals ORDER BY id") as cur:
            periph_rows = await cur.fetchall()
        async with db.execute(
            "SELECT device_id, peripheral_id FROM device_peripherals ORDER BY device_id"
        ) as cur:
            dp_rows = await cur.fetchall()
        async with db.execute(
            "SELECT device_id, vid, pid, role, bcd_device, description, iserial, "
            "       first_seen_at, last_seen_at, learned_from_job, source "
            "FROM device_usb_ids ORDER BY device_id, id"
        ) as cur:
            usb_id_rows = await cur.fetchall()
        strand_rows = comp_rows = route_rows = []
        try:
            async with db.execute("SELECT * FROM strands ORDER BY id") as cur:
                strand_rows = await cur.fetchall()
            async with db.execute("SELECT * FROM strand_components ORDER BY strand_id, id") as cur:
                comp_rows = await cur.fetchall()
            async with db.execute(
                "SELECT device_id, strand_id, mux_channel FROM device_strands "
                "ORDER BY strand_id, mux_channel"
            ) as cur:
                route_rows = await cur.fetchall()
        except Exception:  # noqa: BLE001 - strands tables absent on an old DB
            strand_rows = comp_rows = route_rows = []

    # Build device→peripheral_ids lookup
    device_peripheral_ids: dict[str, list[str]] = {}
    for dp in dp_rows:
        device_peripheral_ids.setdefault(dp["device_id"], []).append(dp["peripheral_id"])

    device_usb_ids: dict[str, list[dict[str, Any]]] = {}
    for u in usb_id_rows:
        device_usb_ids.setdefault(u["device_id"], []).append(
            {
                k: u[k]
                for k in (
                    "vid",
                    "pid",
                    "role",
                    "bcd_device",
                    "description",
                    "iserial",
                    "first_seen_at",
                    "last_seen_at",
                    "learned_from_job",
                    "source",
                )
            }
        )

    def _parse(row: Any, list_cols: list[str]) -> dict[str, Any]:
        d = dict(row)
        for col in list_cols:
            if col in d and d[col]:
                d[col] = json.loads(d[col])
        return d

    devices_out = []
    for d in device_rows:
        dev = _parse(d, ["capabilities_json", "usb_json"])
        dev["peripheral_ids"] = device_peripheral_ids.get(dev["id"], [])
        dev["usb_ids"] = device_usb_ids.get(dev["id"], [])
        devices_out.append(dev)

    # Assemble strands with their components + per-DUT analog-mux routes.
    strand_components: dict[str, list[dict[str, Any]]] = {}
    for c in comp_rows:
        strand_components.setdefault(c["strand_id"], []).append(
            _parse(c, ["ws_types_json", "capabilities_json"])
        )
    strand_routes: dict[str, list[dict[str, Any]]] = {}
    for r in route_rows:
        strand_routes.setdefault(r["strand_id"], []).append(
            {"device": r["device_id"], "channel": r["mux_channel"]}
        )
    strands_out = []
    for s in strand_rows:
        strand = dict(s)
        strand["components"] = strand_components.get(strand["id"], [])
        strand["routes"] = strand_routes.get(strand["id"], [])
        strands_out.append(strand)

    return {
        "hosts": [_parse(h, ["capabilities_json"]) for h in host_rows],
        "devices": devices_out,
        "auxes": [_parse(a, ["capabilities_json"]) for a in aux_rows],
        "connections": [dict(c) for c in conn_rows],
        "peripherals": [_parse(p, ["specs_json"]) for p in periph_rows],
        "strands": strands_out,
    }


@router.get("/export", response_class=PlainTextResponse, include_in_schema=True)
async def export_topology(request: Request, _auth: Auth) -> PlainTextResponse:
    """Backport the live DB to reseedable topology YAML (commit to topology.yaml)."""
    from hil_controller.topology.export import build_export_yaml

    yaml_text = await build_export_yaml(request.app.state.db_path)
    return PlainTextResponse(yaml_text, media_type="application/yaml")


@router.post("/resolve", response_model=ResolveResponse)
async def resolve_topology(request: Request, body: ResolveTarget, _auth: Auth) -> ResolveResponse:
    db_path: str = request.app.state.db_path
    rejected: list[dict[str, Any]] = []

    async with get_db(db_path) as db:
        # Build device filter
        filters = ["d.pool = ?", "d.status = 'available'"]
        params: list[Any] = [body.pool]

        if body.device.id:
            filters.append("d.id = ?")
            params.append(body.device.id)
        if body.device.kind:
            filters.append("d.kind = ?")
            params.append(body.device.kind)
        if body.device.model:
            filters.append("d.model = ?")
            params.append(body.device.model)

        where = "WHERE " + " AND ".join(filters)
        async with db.execute(f"SELECT * FROM devices d {where} ORDER BY d.id", params) as cur:
            device_rows = await cur.fetchall()

        if not device_rows:
            rejected.append(
                {
                    "reason": "no_device",
                    "detail": (
                        f"No available device matched"
                        f"{' id=' + body.device.id if body.device.id else ''}"
                        f"{' kind=' + body.device.kind if body.device.kind else ''}"
                        f"{' model=' + body.device.model if body.device.model else ''}"
                        f" pool={body.pool}"
                    ),
                }
            )

        # Capability filter (post-query, avoids complex JSON SQL)
        if body.device.capabilities:
            filtered = []
            for d in device_rows:
                caps = json.loads(d["capabilities_json"])
                if all(c in caps for c in body.device.capabilities):
                    filtered.append(d)
            if not filtered and device_rows:
                rejected.append(
                    {
                        "reason": "no_capability",
                        "detail": f"No device with capabilities {body.device.capabilities}",
                    }
                )
            device_rows = filtered

        candidates: list[Candidate] = []
        for device in device_rows:
            async with db.execute(
                "SELECT * FROM hosts WHERE id = ? AND status NOT IN ('offline','quarantined')",
                (device["host_id"],),
            ) as cur:
                host_row = await cur.fetchone()
            if host_row is None:
                rejected.append(
                    {
                        "reason": "host_unavailable",
                        "detail": f"Host {device['host_id']} offline or quarantined",
                    }
                )
                continue

            # Aux bindings
            aux_bindings: list[dict[str, Any]] = []
            aux_rejected = False
            for aux_sel in body.requires:
                # Find auxes connected to this device matching the selector
                async with db.execute(
                    """
                    SELECT a.* FROM auxes a
                    JOIN connections c ON c.aux_id = a.id
                    WHERE c.device_id = ? AND a.status = 'available'
                    """,
                    (device["id"],),
                ) as cur:
                    aux_rows = await cur.fetchall()

                matched = []
                for a in aux_rows:
                    caps = json.loads(a["capabilities_json"])
                    if aux_sel.kind and a["kind"] != aux_sel.kind:
                        continue
                    if aux_sel.capabilities and not all(c in caps for c in aux_sel.capabilities):
                        continue
                    matched.append(dict(a))

                if not matched:
                    rejected.append(
                        {
                            "reason": "no_aux",
                            "detail": (
                                f"No aux matched"
                                f"{' kind=' + aux_sel.kind if aux_sel.kind else ''}"
                                f" for device {device['id']}"
                            ),
                        }
                    )
                    aux_rejected = True
                    break
                aux_bindings.append({"selector": aux_sel.model_dump(), "matched": matched[0]})

            if not aux_rejected:
                candidates.append(
                    Candidate(
                        host_id=device["host_id"],
                        device_id=device["id"],
                        aux_bindings=aux_bindings,
                        mux_ops=[],
                    )
                )

    if not candidates:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": "No candidates found", "rejected": rejected},
        )

    return ResolveResponse(candidates=candidates, rejected=rejected)
