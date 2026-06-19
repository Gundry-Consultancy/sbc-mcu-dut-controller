"""GET /healthz, GET /readyz."""

from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> dict:
    db_ok = False
    try:
        db_path: str = request.app.state.db_path
        from hil_controller.db.connection import get_db

        async with get_db(db_path) as db:
            await db.execute("SELECT 1")
        db_ok = True
    except Exception:
        pass

    return {"status": "ok" if db_ok else "degraded", "db": "ok" if db_ok else "error"}
