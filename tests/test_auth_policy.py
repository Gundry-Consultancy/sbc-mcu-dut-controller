"""M2 tests: pool/profile/capabilities gating, audit log."""

import json
import secrets
import uuid
from datetime import UTC

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _mint_token(
    db_path: str,
    *,
    pool: str = "public",
    allowed_pools: list[str] | None = None,
    allowed_profiles: list[str] | None = None,
    default_profile: str = "bench-protomq",
    capabilities: list[str] | None = None,
) -> str:
    """Insert a token directly into the DB and return the plain token string."""
    from datetime import datetime, timezone

    import aiosqlite
    from argon2 import PasswordHasher

    token_id = secrets.token_hex(8)  # hex only — no underscores that would break the split
    secret = secrets.token_urlsafe(32)
    ph = PasswordHasher()
    hashed = ph.hash(secret)
    created_at = datetime.now(UTC).isoformat()

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO tokens
                (id, label, repo, pool, hash, created_at,
                 allowed_pools, allowed_profiles, default_profile, capabilities)
            VALUES (?, ?, '', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token_id,
                "test-token",
                pool,
                hashed,
                created_at,
                json.dumps(allowed_pools or [pool]),
                json.dumps(allowed_profiles or [default_profile]),
                default_profile,
                json.dumps(capabilities or []),
            ),
        )
        await db.commit()
    return f"hil_{token_id}_{secret}"


SBC_JOB = {
    "target": {"device": {"kind": "sbc"}, "pool": "wippersnapper-python"},
    "script": "git-clone-and-run",
    "params": {},
    "secrets_profile": "bench-protomq",
}


# ---------------------------------------------------------------------------
# Pool gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pool_allowed(app):
    token = await _mint_token(
        app.state.db_path,
        pool="wippersnapper-python",
        allowed_pools=["wippersnapper-python"],
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        r = await ac.post("/v1/jobs", json=SBC_JOB)
    assert r.status_code == 202


@pytest.mark.asyncio
async def test_pool_denied(app):
    token = await _mint_token(
        app.state.db_path,
        pool="public",
        allowed_pools=["public"],
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        r = await ac.post("/v1/jobs", json=SBC_JOB)
    assert r.status_code == 403
    assert "wippersnapper-python" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Profile gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_profile_allowed(app):
    token = await _mint_token(
        app.state.db_path,
        pool="wippersnapper-python",
        allowed_pools=["wippersnapper-python"],
        allowed_profiles=["bench-protomq"],
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        r = await ac.post("/v1/jobs", json=SBC_JOB)
    assert r.status_code == 202


@pytest.mark.asyncio
async def test_profile_denied(app):
    token = await _mint_token(
        app.state.db_path,
        pool="wippersnapper-python",
        allowed_pools=["wippersnapper-python"],
        allowed_profiles=["live-io-prod"],
        default_profile="live-io-prod",
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        r = await ac.post("/v1/jobs", json=SBC_JOB)
    assert r.status_code == 403
    assert "bench-protomq" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Capabilities gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trusted_script_denied_without_capability(app):
    token = await _mint_token(
        app.state.db_path,
        pool="wippersnapper-python",
        allowed_pools=["wippersnapper-python"],
        capabilities=[],
    )
    job = {**SBC_JOB, "script": "raw-firmware-smoke"}
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        r = await ac.post("/v1/jobs", json=job)
    assert r.status_code == 403
    assert "trusted-firmware" in r.json()["detail"]


@pytest.mark.asyncio
async def test_trusted_script_allowed_with_capability(app):
    token = await _mint_token(
        app.state.db_path,
        pool="wippersnapper-python",
        allowed_pools=["wippersnapper-python"],
        capabilities=["trusted-firmware"],
    )
    job = {**SBC_JOB, "script": "raw-firmware-smoke"}
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        r = await ac.post("/v1/jobs", json=job)
    assert r.status_code == 202


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_log_written_on_job_submit(app):
    import aiosqlite

    r_submit = None
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer test-token-for-ci"},
    ) as ac:
        r_submit = await ac.post("/v1/jobs", json=SBC_JOB)
    assert r_submit.status_code == 202
    job_id = r_submit.json()["id"]

    async with aiosqlite.connect(app.state.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM audit_log WHERE event = 'job.submit' AND entity_id = ?",
            (job_id,),
        ) as cur:
            row = await cur.fetchone()

    assert row is not None
    assert row["subject"] == "static"


@pytest.mark.asyncio
async def test_audit_log_written_on_auth_fail(app):
    import aiosqlite

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer bad-token-xyz"},
    ) as ac:
        r = await ac.post("/v1/jobs", json=SBC_JOB)
    assert r.status_code == 401

    async with aiosqlite.connect(app.state.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM audit_log WHERE event = 'auth.fail' ORDER BY id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()

    assert row is not None


# ---------------------------------------------------------------------------
# Static token remains a superuser
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_static_token_bypasses_gating(authed_client):
    """The bootstrap static token must be able to submit to any pool/profile."""
    r = await authed_client.post("/v1/jobs", json=SBC_JOB)
    assert r.status_code == 202
