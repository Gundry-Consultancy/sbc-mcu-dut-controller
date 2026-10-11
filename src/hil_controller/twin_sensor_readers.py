"""Explicit sensor identity reads over an already leased and verified I2C route.

No bus implementation, scanning, route selection, state changes or EEPROM programming
is provided here. The caller establishes the exact model and an awake, idle device.
Sources and known limitations are recorded in docs/twin-discovery.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal, Protocol

from pydantic import JsonValue

from hil_controller.twin_models import Identity

ReadStatus = Literal["ok", "unsupported", "blocked", "read_failed", "invalid_response"]


class IdentityBus(Protocol):
    """Raw I2C transport; do not add SMBus lengths, commands, byte swaps or CRCs.

    write/read are separate transactions with STOP. write_read uses a repeated
    START with no intervening STOP. wait must wait at least the given duration.
    Implementations must use bounded I/O timeouts and raise OSError on I/O failure.
    """

    def write(self, address: int, data: bytes) -> None: ...

    def read(self, address: int, length: int) -> bytes: ...

    def write_read(self, address: int, data: bytes, length: int) -> bytes: ...

    def wait(self, seconds: float) -> None: ...


@dataclass(frozen=True)
class SensorReadContext:
    """Caller attestations, not a replacement for controller lease enforcement.

    idle includes awake, completed power-on/reset timing, no conversion in flight
    and no concurrent EEPROM programming. Hold the route lease through the read.
    """

    route_verified: bool
    lease_held: bool
    idle: bool


@dataclass(frozen=True)
class IdentityExchange:
    command: bytes
    expected_bytes: int
    delay_seconds: float | None
    repeated_start: bool
    response: bytes | None = None
    crc_words: tuple[bool, ...] | None = None


@dataclass(frozen=True)
class SensorIdentityResult:
    model: str
    address: int
    status: ReadStatus
    reason: str
    identities: tuple[Identity, ...] = ()
    exchanges: tuple[IdentityExchange, ...] = ()
    attributes: dict[str, JsonValue] = field(default_factory=dict)

    def to_observation_attributes(self) -> dict[str, JsonValue]:
        """JSON-safe evidence to retain alongside an Observation's identities."""
        exchanges: list[JsonValue] = [
            {
                "command_hex": exchange.command.hex(),
                "expected_bytes": exchange.expected_bytes,
                "delay_seconds": exchange.delay_seconds,
                "repeated_start": exchange.repeated_start,
                "response_hex": (
                    exchange.response.hex() if exchange.response is not None else None
                ),
                "crc_words": (list(exchange.crc_words) if exchange.crc_words is not None else None),
            }
            for exchange in self.exchanges
        ]
        return {
            **self.attributes,
            "identity_discovery": {
                "model": self.model,
                "address": self.address,
                "status": self.status,
                "reason": self.reason,
                "exchanges": exchanges,
            },
        }


@dataclass(frozen=True)
class _Recipe:
    vendor: str
    family: str
    commands: tuple[bytes, ...]
    response_bytes: int
    delay_seconds: float | None
    crc: bool
    source: str
    mutable: bool = False


_SGP30_SOURCE = "https://sensirion.com/file/datasheet_sgp30/"
_SGP40_SOURCE = (
    "https://sensirion.com/media/documents/296373BB/6203C5DF/"
    "Sensirion_Gas_Sensors_Datasheet_SGP40.pdf"
)
_SGP41_SOURCE = "https://sensirion.com/resource/datasheet/sgp41"
_HDC_SOURCE = "https://www.ti.com/lit/ds/symlink/hdc3020.pdf"
_STCC_SOURCE = "https://sensirion.github.io/python-i2c-stcc4/api.html"
_TMP_SOURCE = "https://www.ti.com/lit/ds/symlink/tmp119.pdf"

_RECIPES = {
    "sgp30": _Recipe("sensirion", "sgp30", (b"\x36\x82",), 9, 0.0005, True, _SGP30_SOURCE),
    "sgp40": _Recipe("sensirion", "sgp40", (b"\x36\x82",), 9, 0.001, True, _SGP40_SOURCE),
    "sgp41": _Recipe("sensirion", "sgp41", (b"\x36\x82",), 9, 0.001, True, _SGP41_SOURCE),
    "hdc302x": _Recipe(
        "ti", "hdc302x", (b"\x36\x83", b"\x36\x84", b"\x36\x85"), 3, None, True, _HDC_SOURCE
    ),
    "stcc4": _Recipe("sensirion", "stcc4", (b"\x36\x5b",), 18, 0.001, True, _STCC_SOURCE),
    "tmp119": _Recipe(
        "ti", "tmp119", (b"\x05", b"\x06", b"\x08"), 2, None, False, _TMP_SOURCE, mutable=True
    ),
}
for _hdc_model in ("hdc3020", "hdc3021", "hdc3022"):
    _RECIPES[_hdc_model] = _RECIPES["hdc302x"]


class _InvalidResponse(Exception):
    pass


def crc8_word(word: bytes) -> int:
    """CRC-8: polynomial 0x31, initial 0xff, no reflection or final XOR."""
    if len(word) != 2:
        raise ValueError("CRC input must be one two-byte word")
    crc = 0xFF
    for value in word:
        crc ^= value
        for _ in range(8):
            crc = ((crc << 1) ^ (0x31 if crc & 0x80 else 0)) & 0xFF
    return crc


def _read_exchange(
    bus: IdentityBus,
    address: int,
    command: bytes,
    recipe: _Recipe,
    exchanges: list[IdentityExchange],
) -> bytes:
    sample = IdentityExchange(
        command=command,
        expected_bytes=recipe.response_bytes,
        delay_seconds=recipe.delay_seconds,
        repeated_start=recipe.delay_seconds is None,
    )
    exchanges.append(sample)
    if recipe.delay_seconds is None:
        raw = bus.write_read(address, command, recipe.response_bytes)
    else:
        bus.write(address, command)
        bus.wait(recipe.delay_seconds)
        raw = bus.read(address, recipe.response_bytes)
    sample = replace(sample, response=raw)
    exchanges[-1] = sample
    if len(raw) != recipe.response_bytes:
        raise _InvalidResponse(
            f"Response to {command.hex()} has {len(raw)} bytes; expected {recipe.response_bytes}"
        )
    if not recipe.crc:
        return raw
    crc_words = tuple(
        crc8_word(raw[offset : offset + 2]) == raw[offset + 2] for offset in range(0, len(raw), 3)
    )
    exchanges[-1] = replace(sample, crc_words=crc_words)
    if not all(crc_words):
        raise _InvalidResponse(f"CRC validation failed for response to {command.hex()}")
    return b"".join(raw[offset : offset + 2] for offset in range(0, len(raw), 3))


def read_sensor_identity(
    model: str,
    address: int,
    bus: IdentityBus,
    *,
    context: SensorReadContext,
) -> SensorIdentityResult:
    """Read only the selected model; return identities suitable for Observation.

    Unsupported models and unmet preconditions never touch the transport. The
    result retains partial/invalid response evidence, but emits no identities on
    any failure. The caller supplies Observation IDs, timestamps and route
    provenance and performs reconciliation separately. Never automatically retry
    a failure against a different model or address.
    """
    model = model.strip().lower()
    recipe = _RECIPES.get(model)
    if recipe is None:
        reason = "No reviewed identity reader for the explicitly selected model"
        if model == "scd30":
            reason = (
                "Official serial API exists, but raw framing and firmware/state preconditions "
                "have not been vetted for this reader; no command issued"
            )
        return SensorIdentityResult(model, address, "unsupported", reason)
    if (
        not context.route_verified
        or not context.lease_held
        or not context.idle
        or isinstance(address, bool)
        or not isinstance(address, int)
        or not 0x08 <= address <= 0x77
    ):
        return SensorIdentityResult(
            model,
            address,
            "blocked",
            "Requires a valid unreserved 7-bit address, verified route, held lease and idle device",
        )
    exchanges: list[IdentityExchange] = []
    attributes: dict[str, JsonValue] = {
        "identity_reader": "sensor-identity-v1",
        "source": recipe.source,
    }
    try:
        payload = b"".join(
            _read_exchange(bus, address, command, recipe, exchanges) for command in recipe.commands
        )
        if recipe.family == "stcc4":
            product = int.from_bytes(payload[:4], "big")
            attributes["product_id"] = f"{product:08x}"
            if product != 0x0901018A:
                raise _InvalidResponse("Product ID does not match the explicitly selected STCC4")
            payload = payload[4:]
        if recipe.mutable:
            attributes["factory_identity_mutable"] = True
            attributes["eeprom_words"] = {
                f"{command[0]:02x}": raw.response.hex()
                for command, raw in zip(recipe.commands, exchanges, strict=True)
                if raw.response is not None
            }
        if not payload or payload in (bytes(len(payload)), b"\xff" * len(payload)):
            raise _InvalidResponse("Empty or placeholder identity; retain raw evidence only")
    except _InvalidResponse as exc:
        return SensorIdentityResult(
            model,
            address,
            "invalid_response",
            str(exc),
            exchanges=tuple(exchanges),
            attributes=attributes,
        )
    except OSError as exc:
        return SensorIdentityResult(
            model,
            address,
            "read_failed",
            f"{type(exc).__name__}: {exc}",
            exchanges=tuple(exchanges),
            attributes=attributes,
        )
    evidence = (
        f"Explicit {model} identity recipe on caller-verified route; "
        + ("CRC validated for every returned word. " if recipe.crc else "No wire CRC available. ")
        + (
            "Raw EEPROM1/2/3 concatenated in register order; factory identity is mutable, "
            "not verified as original factory contents. "
            if recipe.mutable
            else "Manufacturer-documented unit serial. "
        )
        + f"Source: {recipe.source}"
    )
    identity = Identity(
        namespace=f"{recipe.vendor}:{recipe.family}:"
        + ("factory_eeprom" if recipe.mutable else "serial"),
        kind="manufacturer_serial",
        value=payload.hex(),
        owner="chip",
        component=recipe.family,
        unique=not recipe.mutable,
        confidence="reported" if recipe.mutable else "verified",
        evidence=evidence,
    )
    return SensorIdentityResult(
        model,
        address,
        "ok",
        "Identity read completed",
        identities=(identity,),
        exchanges=tuple(exchanges),
        attributes=attributes,
    )
