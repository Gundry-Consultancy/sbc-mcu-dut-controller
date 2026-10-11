-- Physical inventory is additive: legacy devices remain logical schedulable slots.
CREATE TABLE IF NOT EXISTS physical_twins (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    pool TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS twin_observations (
    observation_id TEXT PRIMARY KEY,
    twin_id TEXT NOT NULL REFERENCES physical_twins(id),
    source TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS twin_observations_twin ON twin_observations(twin_id, observed_at);
CREATE TABLE IF NOT EXISTS twin_identities (
    id INTEGER PRIMARY KEY,
    twin_id TEXT NOT NULL REFERENCES physical_twins(id),
    namespace TEXT NOT NULL,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    owner TEXT NOT NULL,
    component TEXT NOT NULL,
    is_unique INTEGER NOT NULL CHECK(is_unique IN (0,1)),
    confidence TEXT NOT NULL,
    evidence TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    observation_id TEXT NOT NULL REFERENCES twin_observations(observation_id),
    UNIQUE(twin_id, namespace, kind, value, owner, component)
);
CREATE UNIQUE INDEX IF NOT EXISTS twin_identity_unique_claim
    ON twin_identities(namespace, kind, value) WHERE is_unique=1;
CREATE INDEX IF NOT EXISTS twin_identity_lookup ON twin_identities(namespace, kind, value);
CREATE TABLE IF NOT EXISTS twin_ports (
    twin_id TEXT NOT NULL REFERENCES physical_twins(id),
    id TEXT NOT NULL,
    kind TEXT NOT NULL,
    mode TEXT,
    attributes_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    observation_id TEXT NOT NULL REFERENCES twin_observations(observation_id),
    PRIMARY KEY(twin_id, id)
);
CREATE TABLE IF NOT EXISTS twin_slot_assignments (
    id INTEGER PRIMARY KEY,
    slot_kind TEXT NOT NULL,
    slot_id TEXT NOT NULL,
    twin_id TEXT NOT NULL REFERENCES physical_twins(id),
    assigned_at TEXT NOT NULL,
    ended_at TEXT,
    observation_id TEXT NOT NULL REFERENCES twin_observations(observation_id),
    ended_by_observation TEXT REFERENCES twin_observations(observation_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS twin_slot_current
    ON twin_slot_assignments(slot_kind, slot_id) WHERE ended_at IS NULL;
CREATE TABLE IF NOT EXISTS twin_connections (
    id TEXT PRIMARY KEY,
    from_twin_id TEXT NOT NULL,
    from_port TEXT NOT NULL,
    to_twin_id TEXT NOT NULL,
    to_port TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('connected','potential')),
    attributes_json TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence TEXT NOT NULL,
    evidence TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    close_source TEXT,
    close_reason TEXT,
    close_actor TEXT,
    actor TEXT NOT NULL,
    FOREIGN KEY(from_twin_id, from_port) REFERENCES twin_ports(twin_id, id),
    FOREIGN KEY(to_twin_id, to_port) REFERENCES twin_ports(twin_id, id)
);
CREATE INDEX IF NOT EXISTS twin_connections_from ON twin_connections(from_twin_id);
CREATE INDEX IF NOT EXISTS twin_connections_to ON twin_connections(to_twin_id);
