"""Offline, atomic twin import. Defaults to a disposable copy of an existing DB.

Stop writers before applying an inventory maintenance batch. This command does
not initialize a controller database or run unrelated controller migrations.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any

from hil_controller import twins
from hil_controller.db.connection import get_db
from hil_controller.twin_models import Connection, Observation


def snapshot(source: Path, destination: Path) -> None:
    """SQLite online backup, including committed WAL data; never copy the DB file."""
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)


async def import_batch(db_path: str, document: Any, actor: str) -> dict[str, Any]:
    if isinstance(document, list):
        document = {"observations": document}
    if not isinstance(document, dict) or set(document) - {"observations", "connections"}:
        raise ValueError("Expected observation list or observations/connections object")
    observations = [Observation.model_validate(value) for value in document.get("observations", [])]
    connections = [Connection.model_validate(value) for value in document.get("connections", [])]
    results = []
    resolved: dict[str, str] = {}
    async with get_db(db_path) as db:
        schema = Path(__file__).parent / "db" / "twins.sql"
        await db.executescript("BEGIN IMMEDIATE;\n" + schema.read_text(encoding="utf-8-sig"))
        try:
            for observation in observations:
                result = await twins.ingest(db, observation, actor)
                results.append(result)
                resolved["observation:" + observation.observation_id] = result["twin_id"]
            for connection in connections:
                data = connection.model_dump()
                for key in ("from_twin_id", "to_twin_id"):
                    if data[key].startswith("observation:"):
                        if data[key] not in resolved:
                            raise ValueError(
                                "Connection references an observation outside this batch"
                            )
                        data[key] = resolved[data[key]]
                await twins.connect(db, Connection.model_validate(data), actor)
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return {"observations": results, "connections": len(connections)}


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="Existing controller SQLite DB")
    parser.add_argument("--input", required=True, type=Path, help="Observation JSON batch")
    parser.add_argument("--apply", action="store_true", help="Apply to DB; default is dry-run")
    parser.add_argument("--backup", type=Path, help="Required new backup path when applying")
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error("--db must be an existing controller database")
    if args.apply and args.backup is None:
        parser.error("--apply requires --backup with a new backup filename")
    if args.backup is not None and args.backup.exists():
        parser.error("Backup destination already exists; it will not be overwritten")
    try:
        document = json.loads(args.input.read_text(encoding="utf-8-sig"))
        if args.apply:
            # Reserve the name atomically: never overwrite an earlier backup.
            with args.backup.open("xb"):
                pass
            snapshot(args.db, args.backup)
            result = asyncio.run(import_batch(str(args.db), document, "offline-import"))
        else:
            with tempfile.TemporaryDirectory(prefix="hil-twins-preview-") as temporary:
                copied = Path(temporary) / "preview.db"
                snapshot(args.db, copied)
                result = asyncio.run(import_batch(str(copied), document, "offline-preview"))
        print(json.dumps({"applied": args.apply, "result": result}, indent=2))
    except (twins.TwinError, ValueError, sqlite3.Error, OSError) as exc:
        parser.exit(1, f"Twin import failed: {exc}\n")


if __name__ == "__main__":
    cli()
