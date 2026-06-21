"""GET /v1/targets — the device-availability matrix for CI callers.

See docs/device-availability.md. The availability *policy* (how a row renders
into a target record) lives in :mod:`hil_controller.availability`; this module
is the thin HTTP layer that reads the ``devices`` table and applies it.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from hil_controller import availability, host_hardware
from hil_controller.auth.principal import Principal
from hil_controller.auth.tokens import require_auth
from hil_controller.db.connection import get_db

router = APIRouter(prefix="/v1", tags=["targets"])
Auth = Annotated[Principal, Depends(require_auth)]


@router.get("/targets")
async def list_targets(request: Request, _auth: Auth) -> dict[str, Any]:
    """Return the availability matrix: one record per device/target.

    Each record carries its host's detected hardware (real board model, CPU/RAM,
    live load, work-speed score) under ``host`` so callers can distinguish SBC
    hosts that all share the same static device ``model``.
    """
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        cur = await db.execute(
            "SELECT id, host_id, kind, model, build_target, status, unavailable_kind, "
            "unavailable_reason, retry_after FROM devices"
        )
        rows = [dict(r) for r in await cur.fetchall()]
        host_cur = await db.execute(
            "SELECT id, hw_detected_json, hw_override_json, load_json, "
            "speed_score, speed_score_at, specs_detected_at FROM hosts"
        )
        host_hw = {r["id"]: host_hardware.host_hw_view(dict(r)) for r in await host_cur.fetchall()}
    return {
        "targets": [
            availability.target_record(row, host_hw=host_hw.get(row.get("host_id"))) for row in rows
        ]
    }
