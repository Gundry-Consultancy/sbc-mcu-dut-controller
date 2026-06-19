"""Bearer-token authentication — returns a Principal on success.

Bootstrap path: HIL_STATIC_TOKEN env var, accepted as plaintext.
Production path: argon2id-hashed tokens in the DB (see scripts/mint-token.py).
"""

from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from hil_controller.auth.principal import Principal

log = logging.getLogger(__name__)
_bearer = HTTPBearer(auto_error=False)

_SUPERUSER = Principal(
    kind="static",
    subject="static",
    repo="",
    allowed_pools=["*"],
    allowed_profiles=["*"],
    default_profile="bench-protomq",
    capabilities=["*"],
)


def _get_static_token() -> str:
    from hil_controller.config import get_settings

    return get_settings().static_token


async def require_auth(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    """Dependency: validate bearer token, return a Principal."""
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")

    token = credentials.credentials
    static = _get_static_token()

    if static and token == static:
        return _SUPERUSER

    principal = await _check_db_token(request, token)
    if principal is not None:
        return principal

    await _audit_auth_fail(request, token)
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


async def _check_db_token(request: Request, token: str) -> Principal | None:
    try:
        import argon2
        from argon2 import PasswordHasher

        parts = token.split("_", 2)
        if len(parts) != 3 or parts[0] != "hil":
            return None
        token_id, secret = parts[1], parts[2]

        db_path = request.app.state.db_path
        from hil_controller.db.connection import get_db

        async with get_db(db_path) as db:
            async with db.execute(
                "SELECT * FROM tokens WHERE id = ? AND revoked_at IS NULL", (token_id,)
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                return None

        ph = PasswordHasher()
        try:
            ph.verify(row["hash"], secret)
        except argon2.exceptions.VerifyMismatchError:
            return None

        row = dict(row)
        allowed_pools = json.loads(row.get("allowed_pools") or "[]")
        if not allowed_pools:
            allowed_pools = [row["pool"]]

        allowed_profiles = json.loads(row.get("allowed_profiles") or "[]")
        if not allowed_profiles:
            allowed_profiles = [row.get("default_profile") or "bench-protomq"]

        return Principal(
            kind="db-token",
            subject=token_id,
            repo=row.get("repo", ""),
            allowed_pools=allowed_pools,
            allowed_profiles=allowed_profiles,
            default_profile=row.get("default_profile") or "bench-protomq",
            capabilities=json.loads(row.get("capabilities") or "[]"),
        )
    except Exception:
        log.exception("Token DB check failed")
        return None


async def _audit_auth_fail(request: Request, token: str) -> None:
    try:
        db_path = request.app.state.db_path
        from hil_controller.db.connection import audit_event, get_db

        # only log the token id prefix, never the secret
        hint = token[:12] + "..." if len(token) > 12 else token[:4] + "..."
        async with get_db(db_path) as db:
            await audit_event(db, "auth.fail", detail={"hint": hint})
    except Exception:
        pass
