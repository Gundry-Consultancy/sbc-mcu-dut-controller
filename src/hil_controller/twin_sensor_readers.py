"""Explicit sensor identity reads over an already leased and verified I2C route.

No bus implementation, scanning, route selection, state changes or EEPROM programming
is provided here. The caller establishes the exact model and a ready command interface.
Measurement idle is required only by recipes whose documented command needs it.
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

    interface_ready attests awake, completed power-on/reset timing and no pending
    command or EEPROM programming that would prevent this identity transaction.
    It does not assert that background/periodic measurement is stopped.
    measurement_idle separately attests the measurement state for recipes needing it.
    Hold the route lease through the read; these flags never stop/reset a device.

    Legacy idle=True supplies both attestations when the new keyword-only fields
    are omitted. Explicit new fields override that fallback, including False.
    Legacy idle=False retains its previous conservative blocking behavior.
    """

    route_verified: bool
    lease_held: bool
    idle: bool | None = None
    interface_ready: bool | None = field(default=None, kw_only=True)
    measurement_idle: bool | None = field(default=None, kw_only=True)

    @property
    def ready_interface(self) -> bool:
        return self.interface_ready if self.interface_ready is not None else self.idle is True

    @property
    def idle_measurement(self) -> bool | None:
        return self.measurement_idle if self.measurement_idle is not None else self.idle


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
    requires_measurement_idle: bool = False


_SGP30_SOURCE = "https://sensirion.com/file/datasheet_sgp30/"
_SGP40_SOURCE = (
    "https://sensirion.com/media/documents/296373BB/6203C5DF/"
    "Sensirion_Gas_Sensors_Datasheet_SGP40.pdf"
)
_SGP41_SOURCE = "https://sensirion.com/resource/datasheet/sgp41"
_HDC_SOURCE = "https://www.ti.com/lit/ds/symlink/hdc3020.pdf"
_STCC_SOURCE = "https://sensirion.github.io/python-i2c-stcc4/api.html"
_TMP_SOURCE = "https://www.ti.com/lit/ds/symlink/tmp119.pdf"
_SCD30_SOURCE = (
    "https://github.com/Sensirion/embedded-i2c-scd30/blob/"
    "ebe6d25cff6b6f317b639fded3a68ec71d44e178/scd30_i2c.c"
)

_RECIPES = {
    "sgp30": _Recipe("sensirion", "sgp30", (b"\x36\x82",), 9, 0.0005, True, _SGP30_SOURCE),
    "sgp40": _Recipe(
        "sensirion",
        "sgp40",
        (b"\x36\x82",),
        9,
        0.001,
        True,
        _SGP40_SOURCE,
        requires_measurement_idle=True,
    ),
    "sgp41": _Recipe(
        "sensirion",
        "sgp41",
        (b"\x36\x82",),
        9,
        0.001,
        True,
        _SGP41_SOURCE,
        requires_measurement_idle=True,
    ),
    "hdc302x": _Recipe(
        "ti", "hdc302x", (b"\x36\x83", b"\x36\x84", b"\x36\x85"), 3, None, True, _HDC_SOURCE
    ),
    "stcc4": _Recipe(
        "sensirion",
        "stcc4",
        (b"\x36\x5b",),
        18,
        0.001,
        True,
        _STCC_SOURCE,
        requires_measurement_idle=True,
    ),
    "scd30": _Recipe("sensirion", "scd30", (b"\xd0\x33",), 48, 0.010, True, _SCD30_SOURCE),
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
        return SensorIdentityResult(model, address, "unsupported", reason)
    attributes: dict[str, JsonValue] = {
        "identity_reader": "sensor-identity-v2",
        "source": recipe.source,
        "read_context": {
            "route_verified": context.route_verified,
            "lease_held": context.lease_held,
            "interface_ready": context.ready_interface,
            "measurement_idle": context.idle_measurement,
            "legacy_idle": context.idle,
            "requires_measurement_idle": recipe.requires_measurement_idle,
        },
    }
    if (
        not context.route_verified
        or not context.lease_held
        or not context.ready_interface
        or isinstance(address, bool)
        or not isinstance(address, int)
        or not 0x08 <= address <= 0x77
    ):
        return SensorIdentityResult(
            model,
            address,
            "blocked",
            "Requires a valid unreserved 7-bit address, verified route, held lease "
            "and ready command interface",
            attributes=attributes,
        )
    if recipe.requires_measurement_idle and context.idle_measurement is not True:
        return SensorIdentityResult(
            model,
            address,
            "blocked",
            "This identity command requires an explicitly attested idle measurement state",
            attributes=attributes,
        )
    exchanges: list[IdentityExchange] = []
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
        serial_text = None
        if recipe.family == "scd30":
            if b"\x00" not in payload:
                raise _InvalidResponse("SCD30 serial lacks its documented ASCII NUL terminator")
            serial_bytes = payload.split(b"\x00", 1)[0]
            if (
                not serial_bytes
                or any(value < 0x20 or value > 0x7E for value in serial_bytes)
                or serial_bytes.strip() != serial_bytes
            ):
                raise _InvalidResponse("SCD30 serial is empty or not printable ASCII")
            serial_text = serial_bytes.decode("ascii")
            attributes["serial_encoding"] = "ascii"
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
        value=serial_text if serial_text is not None else payload.hex(),
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
