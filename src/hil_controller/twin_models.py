"""Validated observation contracts. Locators and model IDs are never unit identities."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

Text = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^\S(?:.*\S)?$")]
Confidence = Literal["verified", "reported", "inferred"]
SlotKind = Literal["device", "host", "strand_component", "peripheral", "aux", "camera"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Identity(StrictModel):
    namespace: Text
    kind: Literal[
        "manufacturer_serial", "silicon_uid", "board_serial", "endpoint_serial", "inventory_tag"
    ]
    value: Text
    owner: Literal["board", "chip", "endpoint"]
    component: Text
    unique: bool = False
    confidence: Confidence = "reported"
    evidence: Annotated[str, Field(min_length=1, max_length=4096)]

    @model_validator(mode="after")
    def ownership(self) -> Self:
        if self.kind == "endpoint_serial" and self.owner != "endpoint":
            raise ValueError("endpoint_serial must belong to an endpoint")
        if self.owner == "endpoint" and self.kind != "endpoint_serial":
            raise ValueError("endpoint observations must use endpoint_serial")
        if self.kind == "silicon_uid" and self.owner != "chip":
            raise ValueError("silicon_uid must belong to a named chip")
        if self.kind == "board_serial" and self.owner != "board":
            raise ValueError("board_serial must belong to a board")
        if self.unique and self.confidence != "verified":
            raise ValueError("unique claims require verified evidence")
        return self


class Port(StrictModel):
    id: Text
    kind: Text
    mode: Text | None = None
    attributes: dict[str, JsonValue] = Field(default_factory=dict)


class Slot(StrictModel):
    kind: SlotKind
    id: Text
    # Mandatory compare-and-swap: null explicitly asserts that the slot is empty.
    expected_twin_id: Text | None


class Observation(StrictModel):
    observation_id: Text
    source: Text
    observed_at: AwareDatetime
    twin_id: Text | None = None
    kind: Text | None = None
    label: Text | None = None
    pool: Text = "public"
    identities: list[Identity] = Field(default_factory=list, max_length=128)
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
    ports: list[Port] = Field(default_factory=list, max_length=128)
    slot: Slot | None = None

    @model_validator(mode="after")
    def valid_creation(self) -> Self:
        if self.twin_id is None and (not self.kind or not self.label):
            raise ValueError("new twins require kind and label")
        if len({p.id for p in self.ports}) != len(self.ports):
            raise ValueError("port IDs must be distinct within an observation")
        return self


class Connection(StrictModel):
    id: Text
    from_twin_id: Text
    from_port: Text
    to_twin_id: Text
    to_port: Text
    kind: Text
    state: Literal["connected", "potential"] = "connected"
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
    source: Text
    confidence: Confidence = "reported"
    evidence: Annotated[str, Field(min_length=1, max_length=4096)]
    started_at: AwareDatetime


class CloseConnection(StrictModel):
    ended_at: AwareDatetime
    source: Text
    reason: Annotated[str, Field(min_length=1, max_length=4096)]
