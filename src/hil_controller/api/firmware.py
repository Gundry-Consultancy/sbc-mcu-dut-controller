"""POST /v1/firmware — upload a firmware image for firmware-bench to flash.

firmware-bench copies ``params.firmware.path`` (a path on the controller) to the
bench. CI that has the ``.bin`` locally (e.g. a PR build artifact, which is not a
public URL) uploads it here; the controller stores it and returns a ``path`` the
job then passes as ``params.firmware.path``. Public release assets can instead
use ``params.firmware.url`` (downloaded controller-side).

Raw request body (``--data-binary @combined.bin``) so no multipart dependency;
the filename comes from a query param.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from hil_controller.adapters.firmware_fetch import store_uploaded_firmware
from hil_controller.auth.principal import Principal
from hil_controller.auth.tokens import require_auth
from hil_controller.db.connection import get_db

router = APIRouter(prefix="/v1", tags=["firmware"])

Auth = Annotated[Principal, Depends(require_auth)]


@router.post("/firmware")
async def upload_firmware(
    request: Request,
    _auth: Auth,
    filename: str = Query("firmware.bin", description="stored filename for the upload"),
) -> dict[str, Any]:
    """Store an uploaded firmware blob; return ``{id, filename, path, size_bytes, sha256}``.

    The returned ``path`` is what a job passes as ``params.firmware.path``. The
    upload is also recorded as a ``kind='firmware'`` asset (job_id NULL until a
    job flashes it, then linked by firmware-bench) with a ``purge_at`` so it
    appears on the Assets list and gets cleaned up eventually.
    """
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty firmware upload")
    db_path: str = request.app.state.db_path
    store_dir = Path(db_path).parent / "firmware_uploads"
    rec = store_uploaded_firmware(data, store_dir=store_dir, filename=filename)
    now = datetime.now(UTC)
    purge_days = int(os.environ.get("HIL_FIRMWARE_PURGE_DAYS", "7"))
    purge_at = (now + timedelta(days=purge_days)).isoformat() if purge_days > 0 else None
    async with get_db(db_path) as db:
        await db.execute(
            "INSERT INTO assets (id, filename, path, size_bytes, kind, job_id, created_at, purge_at) "  # noqa: E501
            "VALUES (?, ?, ?, ?, 'firmware', NULL, ?, ?)",
            (rec["id"], rec["filename"], rec["path"], rec["size_bytes"], now.isoformat(), purge_at),
        )
        await db.commit()
    rec["asset_id"] = rec["id"]
    rec["purge_at"] = purge_at
    return rec
