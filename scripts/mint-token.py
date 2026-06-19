#!/usr/bin/env python3
"""
Mint a new HIL controller bearer token and insert it into the SQLite DB.

Usage:
    python scripts/mint-token.py --db /var/lib/hil/jobs.db \
        --label "ws-python-ci" --pool wippersnapper-python

Prints the plain token once (format: hil_<id>_<secret>).
The DB stores only the argon2id hash; the plain token is never logged.
"""

import argparse
import asyncio
import secrets
import sys
import uuid
from datetime import datetime, timezone


def main() -> None:
    p = argparse.ArgumentParser(description="Mint a HIL API token")
    p.add_argument("--db", required=True, help="Path to the SQLite DB")
    p.add_argument("--label", required=True, help="Human label for this token")
    p.add_argument("--pool", default="public", help="Primary device pool (legacy; prefer --allowed-pools)")
    p.add_argument("--repo", default="", help="Pin token to a specific repo (owner/name)")
    p.add_argument(
        "--allowed-pools",
        default="",
        help="Comma-separated pools this token may target (default: --pool value)",
    )
    p.add_argument(
        "--allowed-profiles",
        default="bench-protomq",
        help="Comma-separated secrets profiles this token may use",
    )
    p.add_argument(
        "--default-profile",
        default="bench-protomq",
        help="Profile applied when the caller omits secrets_profile",
    )
    p.add_argument(
        "--capabilities",
        default="",
        help="Comma-separated capability grants, e.g. trusted-firmware",
    )
    args = p.parse_args()

    asyncio.run(_mint(args))


async def _mint(args: argparse.Namespace) -> None:
    try:
        from argon2 import PasswordHasher
    except ImportError:
        print("ERROR: argon2-cffi not installed. Run: pip install argon2-cffi", file=sys.stderr)
        sys.exit(1)

    try:
        import aiosqlite
    except ImportError:
        print("ERROR: aiosqlite not installed. Run: pip install aiosqlite", file=sys.stderr)
        sys.exit(1)

    import json

    token_id = secrets.token_hex(8)   # hex only — no underscores that would break the split
    secret = secrets.token_urlsafe(32)
    plain_token = f"hil_{token_id}_{secret}"

    ph = PasswordHasher()
    hashed = ph.hash(secret)

    created_at = datetime.now(timezone.utc).isoformat()

    allowed_pools_raw = args.allowed_pools or args.pool
    allowed_pools = [p.strip() for p in allowed_pools_raw.split(",") if p.strip()]
    allowed_profiles = [p.strip() for p in args.allowed_profiles.split(",") if p.strip()]
    capabilities = [c.strip() for c in args.capabilities.split(",") if c.strip()]

    async with aiosqlite.connect(args.db) as db:
        await db.execute(
            """
            INSERT INTO tokens
                (id, label, repo, pool, hash, created_at,
                 allowed_pools, allowed_profiles, default_profile, capabilities)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token_id,
                args.label,
                args.repo,
                args.pool,
                hashed,
                created_at,
                json.dumps(allowed_pools),
                json.dumps(allowed_profiles),
                args.default_profile,
                json.dumps(capabilities),
            ),
        )
        await db.commit()

    print(plain_token)
    print(f"\nToken ID         : {token_id}")
    print(f"Label            : {args.label}")
    print(f"Allowed pools    : {allowed_pools}")
    print(f"Allowed profiles : {allowed_profiles}")
    print(f"Default profile  : {args.default_profile}")
    print(f"Capabilities     : {capabilities or '(none)'}")
    print(f"Repo pin         : {args.repo or '(any)'}")
    print("\nStore this token in your CI secret HIL_API_TOKEN. It will not be shown again.")


if __name__ == "__main__":
    main()
