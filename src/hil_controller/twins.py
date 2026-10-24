"""Physical twin persistence. Call mutations inside BEGIN IMMEDIATE transactions.

No locator, model, or USB descriptor can select a physical unit. A caller explicitly
chooses a twin after reviewing verified identity candidates from preview().
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import aiosqlite

from hil_controller.twin_models import Connection, Observation

SLOT_TABLES = {
    "device": "devices",
    "host": "hosts",
    "strand_component": "strand_components",
    "peripheral": "peripherals",
    "aux": "auxes",
    "camera": "cameras",
}


class TwinError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(detail)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def encoded(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def payload(observation: Observation) -> str:
    data = observation.model_dump(mode="json")
    data["observed_at"] = iso(observation.observed_at)
    return encoded(data)


async def rows(
    db: aiosqlite.Connection, sql: str, args: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
    async with db.execute(sql, args) as cursor:
        return [dict(row) for row in await cursor.fetchall()]


async def require_twin(db: aiosqlite.Connection, twin_id: str) -> dict[str, Any]:
    found = await rows(db, "SELECT * FROM physical_twins WHERE id=?", (twin_id,))
    if not found:
        raise TwinError(404, "Physical twin not found")
    return found[0]


async def candidates(db: aiosqlite.Connection, observation: Observation) -> list[dict[str, Any]]:
    matches: dict[str, list[dict[str, str]]] = {}
    for identity in observation.identities:
        if not identity.unique or identity.confidence != "verified":
            continue
        found = await rows(
            db,
            "SELECT twin_id FROM twin_identities WHERE namespace=? AND kind=? "
            "AND value=? AND owner=? AND is_unique=1 AND confidence='verified'",
            (identity.namespace, identity.kind, identity.value, identity.owner),
        )
        for item in found:
            matches.setdefault(item["twin_id"], []).append(
                {
                    "namespace": identity.namespace,
                    "kind": identity.kind,
                    "value": identity.value,
                }
            )
    return [{"twin_id": key, "matched_identities": value} for key, value in sorted(matches.items())]


async def ingest(db: aiosqlite.Connection, observation: Observation, actor: str) -> dict[str, Any]:
    body = payload(observation)
    previous = await rows(
        db, "SELECT * FROM twin_observations WHERE observation_id=?", (observation.observation_id,)
    )
    if previous:
        if previous[0]["payload_json"] != body:
            raise TwinError(409, "Observation ID already used with different content")
        return {
            "twin_id": previous[0]["twin_id"],
            "observation_id": observation.observation_id,
            "replayed": True,
        }
    at = iso(observation.observed_at)
    if observation.twin_id is not None:
        twin = await require_twin(db, observation.twin_id)
        if observation.pool != twin["pool"]:
            raise TwinError(409, "Observation pool differs from physical twin pool")
        if observation.kind is not None and observation.kind != twin["kind"]:
            raise TwinError(409, "Physical kind is immutable; create a replacement twin")
        twin_id = observation.twin_id
    else:
        if await candidates(db, observation):
            raise TwinError(
                409, "Verified identity already known; preview and choose explicit twin_id"
            )
        twin_id = str(uuid4())
        await db.execute(
            "INSERT INTO physical_twins VALUES (?,?,?,?,?)",
            (twin_id, observation.kind, observation.label, observation.pool, at),
        )
    await db.execute(
        "INSERT INTO twin_observations VALUES (?,?,?,?,?,?,?)",
        (
            observation.observation_id,
            twin_id,
            observation.source,
            at,
            iso(datetime.now(UTC)),
            actor,
            body,
        ),
    )
    for port in observation.ports:
        old = await rows(
            db, "SELECT kind FROM twin_ports WHERE twin_id=? AND id=?", (twin_id, port.id)
        )
        if old and old[0]["kind"] != port.kind:
            raise TwinError(409, "Port kind is immutable; use a distinct port ID")
        await db.execute(
            "INSERT INTO twin_ports VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(twin_id,id) DO UPDATE SET mode=excluded.mode, "
            "attributes_json=excluded.attributes_json, observed_at=excluded.observed_at, "
            "observation_id=excluded.observation_id WHERE excluded.observed_at > observed_at "
            "OR (excluded.observed_at=observed_at AND excluded.observation_id>observation_id)",
            (
                twin_id,
                port.id,
                port.kind,
                port.mode,
                encoded(port.attributes),
                at,
                observation.observation_id,
            ),
        )
    for identity in observation.identities:
        if identity.owner == "endpoint":
            found = await rows(
                db,
                "SELECT id FROM twin_ports WHERE twin_id=? AND id=?",
                (twin_id, identity.component),
            )
            if not found:
                raise TwinError(422, "Endpoint identity component must reference an owned port")
        # A claimed unique identifier on another unit is a conflict, never an automatic merge.
        conflicts = await rows(
            db,
            "SELECT twin_id FROM twin_identities WHERE namespace=? AND kind=? AND value=? "
            "AND (twin_id<>? OR owner<>? OR component<>?) AND (is_unique=1 OR ?=1)",
            (
                identity.namespace,
                identity.kind,
                identity.value,
                twin_id,
                identity.owner,
                identity.component,
                int(identity.unique),
            ),
        )
        if conflicts:
            raise TwinError(409, "Identity is claimed by another physical twin")
        # Do not silently turn one physical specimen into another on an explicit ID either.
        conflicts = await rows(
            db,
            "SELECT value FROM twin_identities WHERE twin_id=? AND namespace=? AND kind=? "
            "AND owner=? AND component=? AND value<>? AND (is_unique=1 OR ?=1)",
            (
                twin_id,
                identity.namespace,
                identity.kind,
                identity.owner,
                identity.component,
                identity.value,
                int(identity.unique),
            ),
        )
        if conflicts:
            raise TwinError(409, "Owned unique identity changed; create a replacement twin")
        await db.execute(
            "INSERT INTO twin_identities(twin_id,namespace,kind,value,owner,component,is_unique,"
            "confidence,evidence,first_seen_at,last_seen_at,observation_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(twin_id,namespace,kind,value,owner,component) DO UPDATE SET "
            "first_seen_at=MIN(first_seen_at,excluded.first_seen_at), "
            "last_seen_at=MAX(last_seen_at,excluded.last_seen_at), "
            "is_unique=MAX(is_unique,excluded.is_unique), "
            "confidence=CASE WHEN excluded.confidence='verified' OR "
            "(confidence='inferred' AND excluded.confidence='reported') "
            "THEN excluded.confidence ELSE confidence END, "
            "evidence=CASE WHEN excluded.confidence='verified' OR "
            "(confidence='inferred' AND excluded.confidence='reported') "
            "THEN excluded.evidence ELSE evidence END, "
            "observation_id=CASE WHEN excluded.confidence='verified' OR "
            "(confidence='inferred' AND excluded.confidence='reported') "
            "THEN excluded.observation_id ELSE observation_id END",
            (
                twin_id,
                identity.namespace,
                identity.kind,
                identity.value,
                identity.owner,
                identity.component,
                int(identity.unique),
                identity.confidence,
                identity.evidence,
                at,
                at,
                observation.observation_id,
            ),
        )
    if observation.slot is not None:
        slot = observation.slot
        table = SLOT_TABLES[slot.kind]  # fixed allowlist, never caller SQL
        found = await rows(db, f"SELECT id FROM {table} WHERE id=?", (slot.id,))
        if not found:
            raise TwinError(422, "Logical slot does not exist in runtime topology")
        current = await rows(
            db,
            "SELECT * FROM twin_slot_assignments WHERE slot_kind=? "
            "AND slot_id=? AND ended_at IS NULL",
            (slot.kind, slot.id),
        )
        current_id = current[0]["twin_id"] if current else None
        if current_id != slot.expected_twin_id:
            raise TwinError(409, "Slot occupant changed; refresh expected_twin_id")
        if current_id != twin_id:
            if current and at <= current[0]["assigned_at"]:
                raise TwinError(409, "Replacement must be later than current assignment")
            await db.execute(
                "UPDATE twin_slot_assignments SET ended_at=?,ended_by_observation=? "
                "WHERE slot_kind=? AND slot_id=? AND ended_at IS NULL",
                (at, observation.observation_id, slot.kind, slot.id),
            )
            await db.execute(
                "INSERT INTO twin_slot_assignments(slot_kind,slot_id,twin_id,"
                "assigned_at,observation_id) VALUES (?,?,?,?,?)",
                (slot.kind, slot.id, twin_id, at, observation.observation_id),
            )
    return {"twin_id": twin_id, "observation_id": observation.observation_id, "replayed": False}


async def detail(db: aiosqlite.Connection, twin_id: str) -> dict[str, Any]:
    twin = await require_twin(db, twin_id)
    observations = await rows(
        db,
        "SELECT * FROM twin_observations WHERE twin_id=? ORDER BY observed_at,observation_id",
        (twin_id,),
    )
    attributes: dict[str, Any] = {}
    for observation in observations:
        data = json.loads(observation.pop("payload_json"))
        attributes.update(data["attributes"])
        if data["label"] is not None:
            twin["label"] = data["label"]
        observation["payload"] = data
    twin["attributes"] = attributes
    twin["observations"] = observations
    twin["identities"] = await rows(
        db, "SELECT * FROM twin_identities WHERE twin_id=? ORDER BY id", (twin_id,)
    )
    for identity in twin["identities"]:
        identity["unique"] = bool(identity.pop("is_unique"))
    twin["ports"] = await rows(
        db, "SELECT * FROM twin_ports WHERE twin_id=? ORDER BY id", (twin_id,)
    )
    for port in twin["ports"]:
        port["attributes"] = json.loads(port.pop("attributes_json"))
    twin["slot_history"] = await rows(
        db,
        "SELECT * FROM twin_slot_assignments WHERE twin_id=? ORDER BY assigned_at,id",
        (twin_id,),
    )
    twin["connections"] = await rows(
        db,
        "SELECT * FROM twin_connections WHERE from_twin_id=? "
        "OR to_twin_id=? ORDER BY started_at,id",
        (twin_id, twin_id),
    )
    for connection in twin["connections"]:
        connection["attributes"] = json.loads(connection.pop("attributes_json"))
    return twin


async def connect(db: aiosqlite.Connection, item: Connection, actor: str) -> dict[str, Any]:
    values = item.model_dump(mode="json")
    values["started_at"] = iso(item.started_at)
    values["attributes_json"] = encoded(values.pop("attributes"))
    existing = await rows(db, "SELECT * FROM twin_connections WHERE id=?", (item.id,))
    if existing:
        if any(existing[0][key] != value for key, value in values.items()):
            raise TwinError(409, "Connection ID already used with different content")
        return existing[0]
    for twin_id, port in [(item.from_twin_id, item.from_port), (item.to_twin_id, item.to_port)]:
        await require_twin(db, twin_id)
        if not await rows(
            db, "SELECT id FROM twin_ports WHERE twin_id=? AND id=?", (twin_id, port)
        ):
            raise TwinError(422, "Connection references an unknown owned port")
    values["actor"] = actor
    await db.execute(
        f"INSERT INTO twin_connections({','.join(values)}) "
        f"VALUES ({','.join('?' for _ in values)})",
        tuple(values.values()),
    )
    return (await rows(db, "SELECT * FROM twin_connections WHERE id=?", (item.id,)))[0]
