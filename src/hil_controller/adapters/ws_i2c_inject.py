"""Inject a v2 WipperSnapper I2C ``Probe`` (and mux ``Add``) via the protomq broker.

Completes the "read the muxed sensors" open item for the QT Py ESP32-S3 N4R2
mux'd-I2C HIL plan: it drives the *running* v2 firmware's I2C controller over the
v2 signal path, so no CircuitPython mux-latch dance is needed.

The v2 firmware **drives the TCA9548A itself**: a ``Probe`` whose ``AddressSpace``
carries ``{mux_address, mux_channel}`` makes ``I2cHardware::ProbeAddresses`` call
``SelectMuxChannel(channel)`` before scanning that space (and clears it after).
So "change the scanned mux channel" is just a different ``mux_channel`` in the
next Probe — the firmware latches it. (A mux must first be registered on the bus
with an ``Add`` named ``pca9548``/``pca9546``, else ProbeAddresses errors
"AddressSpace specifies MUX but none on bus".)

Transport: v2 wraps every component message in ``ws.signal.BrokerToDevice`` and
routes it on ``<io_user>/ws-b2d/<device_uid>``; the device replies on
``<io_user>/ws-d2b/<device_uid>``. We publish the B2D via protomq's
``POST /api/echo`` (same path the v1 pixelWrite injector uses) and capture the
D2B over MQTT.

Wire facts are taken from the firmware's OWN nanopb headers on branch
``migrate-api-v2-backport-components`` (``src/protos/i2c.pb.h`` +
``src/protos/signal.pb.h``) — these differ from the standalone ``.proto`` (e.g.
DeviceToBroker.i2c is field **34**, not 38), so the firmware headers are the
ground truth::

    ws.signal.BrokerToDevice.i2c = field 38   (broker -> device)
    ws.signal.DeviceToBroker.i2c = field 34   (device -> broker)
    ws.i2c.B2D  { probe = 1, add = 2, remove = 3 }
    ws.i2c.D2B  { probed = 1, event = 2 }
    ws.i2c.Probe { address_spaces = 1 (repeated msg), addresses = 2 (repeated uint32) }
    ws.i2c.AddressSpace { pin_scl = 1, pin_sda = 2, mux_address = 3, mux_channel = 4 }  (uint32)
    ws.i2c.Add { descriptor = 1, name = 2, period = 3, types = 4, settings = 5 }
    ws.i2c.Descriptor { address_space = 1 (msg), address = 2 (uint32) }
    ws.i2c.Probed { results = 1 (repeated AddressSpaceResult) }
    ws.i2c.AddressSpaceResult { address_space = 1 (msg), found_addresses = 2 (repeated uint32) }

The firmware skips reserved addresses (``<=0x07`` and ``>=0x78``); probing the
full ``0x08..0x77`` range (112 addrs = ``MAX_PROBE_ADDRESSES``) is the v1-style
full scan.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

try:
    import aiomqtt

    _AIOMQTT_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only where aiomqtt is absent
    _AIOMQTT_AVAILABLE = False

# ws.signal envelope field numbers (firmware headers, ground truth).
_SIGNAL_B2D_I2C = 38  # BrokerToDevice.i2c
_SIGNAL_D2B_I2C = 34  # DeviceToBroker.i2c

# Full scannable 7-bit range (firmware skips <=0x07 and >=0x78 itself).
DEFAULT_PROBE_ADDRESSES = tuple(range(0x08, 0x78))  # 0x08..0x77 = 112 addresses


# --------------------------------------------------------------------------- #
# protobuf wire helpers (encode)                                              #
# --------------------------------------------------------------------------- #
def _varint(n: int) -> bytes:
    """Encode an unsigned int as a protobuf base-128 varint."""
    if n < 0:
        raise ValueError("varint must be non-negative")
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            break
    return bytes(out)


def _tag(field: int, wiretype: int) -> bytes:
    return _varint((field << 3) | wiretype)


def _len_field(field: int, payload: bytes) -> bytes:
    """A length-delimited (wiretype 2) field: tag + len + bytes."""
    return _tag(field, 2) + _varint(len(payload)) + payload


def _vint_field(field: int, value: int) -> bytes:
    """A varint (wiretype 0) field: tag + value."""
    return _tag(field, 0) + _varint(value)


def encode_address_space(
    *, pin_scl: int, pin_sda: int, mux_address: int = 0, mux_channel: int = 0
) -> bytes:
    """Serialize ``ws.i2c.AddressSpace``. ``mux_channel`` is emitted only when a
    ``mux_address`` is set (a bare-bus space carries no mux fields)."""
    b = bytearray()
    if pin_scl:
        b += _vint_field(1, pin_scl)
    if pin_sda:
        b += _vint_field(2, pin_sda)
    if mux_address:
        b += _vint_field(3, mux_address)
        b += _vint_field(4, mux_channel)  # explicit even for channel 0
    return bytes(b)


def encode_probe(address_spaces: list[bytes], addresses: list[int]) -> bytes:
    """Serialize ``ws.i2c.Probe`` (repeated AddressSpace + repeated uint32, unpacked)."""
    b = bytearray()
    for space in address_spaces:
        b += _len_field(1, space)
    for addr in addresses:
        b += _vint_field(2, addr)
    return bytes(b)


def encode_b2d_probe(probe: bytes) -> bytes:
    return _len_field(1, probe)  # ws.i2c.B2D.probe


def encode_descriptor(address_space: bytes, address: int) -> bytes:
    b = bytearray()
    b += _len_field(1, address_space)
    if address:
        b += _vint_field(2, address)
    return bytes(b)


def encode_add(descriptor: bytes, name: str) -> bytes:
    b = bytearray()
    b += _len_field(1, descriptor)
    b += _len_field(2, name.encode("ascii"))
    return bytes(b)


def encode_b2d_add(add: bytes) -> bytes:
    return _len_field(2, add)  # ws.i2c.B2D.add


def encode_signal_i2c(b2d: bytes) -> bytes:
    """Wrap a ``ws.i2c.B2D`` in ``ws.signal.BrokerToDevice`` (field 38)."""
    return _len_field(_SIGNAL_B2D_I2C, b2d)


def build_add_mux(
    *, pin_scl: int, pin_sda: int, mux_address: int, name: str = "pca9548"
) -> bytes:
    """BrokerToDevice payload that registers a TCA9548A/PCA954x on the bus."""
    space = encode_address_space(pin_scl=pin_scl, pin_sda=pin_sda, mux_address=mux_address)
    descriptor = encode_descriptor(space, address=mux_address)
    return encode_signal_i2c(encode_b2d_add(encode_add(descriptor, name)))


def build_probe(
    *,
    pin_scl: int,
    pin_sda: int,
    addresses: list[int],
    mux_address: int = 0,
    mux_channel: int = 0,
) -> bytes:
    """BrokerToDevice payload that probes one AddressSpace (bare bus or one mux channel)."""
    space = encode_address_space(
        pin_scl=pin_scl, pin_sda=pin_sda, mux_address=mux_address, mux_channel=mux_channel
    )
    return encode_signal_i2c(encode_b2d_probe(encode_probe([space], addresses)))


# --------------------------------------------------------------------------- #
# protobuf wire helpers (decode)                                              #
# --------------------------------------------------------------------------- #
def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = 0
    val = 0
    while True:
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, i
        shift += 7


def _walk(buf: bytes) -> list[tuple[int, int, Any]]:
    """Yield ``(field_number, wiretype, value)`` for each top-level field.

    Value is an int for wiretype 0, raw bytes for wiretype 2, and the raw fixed
    bytes for 1/5. Tolerant enough to handle both packed and unpacked repeated
    scalars (the caller inspects the wiretype)."""
    out: list[tuple[int, int, Any]] = []
    i = 0
    n = len(buf)
    while i < n:
        key, i = _read_varint(buf, i)
        field, wt = key >> 3, key & 0x07
        if wt == 0:
            val, i = _read_varint(buf, i)
            out.append((field, 0, val))
        elif wt == 2:
            ln, i = _read_varint(buf, i)
            out.append((field, 2, buf[i : i + ln]))
            i += ln
        elif wt == 5:
            out.append((field, 5, buf[i : i + 4]))
            i += 4
        elif wt == 1:
            out.append((field, 1, buf[i : i + 8]))
            i += 8
        else:  # pragma: no cover - groups are not used in these messages
            raise ValueError(f"unsupported wiretype {wt} at offset {i}")
    return out


def parse_probed(signal_bytes: bytes) -> list[dict[str, Any]]:
    """Decode a ``ws.signal.DeviceToBroker`` and return the Probed results.

    Returns a list of ``{"mux_address", "mux_channel", "found": [addr, ...]}``.
    Returns ``[]`` if the message is not an i2c Probed (e.g. a checkin/event)."""
    results: list[dict[str, Any]] = []
    for f, wt, v in _walk(signal_bytes):
        if f != _SIGNAL_D2B_I2C or wt != 2:
            continue
        for f2, wt2, v2 in _walk(v):  # ws.i2c.D2B
            if f2 != 1 or wt2 != 2:  # probed
                continue
            for f3, wt3, v3 in _walk(v2):  # ws.i2c.Probed
                if f3 != 1 or wt3 != 2:  # results (AddressSpaceResult)
                    continue
                entry: dict[str, Any] = {"mux_address": 0, "mux_channel": None, "found": []}
                for f4, wt4, v4 in _walk(v3):
                    if f4 == 1 and wt4 == 2:  # address_space
                        for f5, wt5, v5 in _walk(v4):
                            if f5 == 3 and wt5 == 0:
                                entry["mux_address"] = v5
                            elif f5 == 4 and wt5 == 0:
                                entry["mux_channel"] = v5
                    elif f4 == 2:  # found_addresses (repeated uint32)
                        if wt4 == 0:  # unpacked
                            entry["found"].append(v4)
                        elif wt4 == 2:  # packed
                            j = 0
                            while j < len(v4):
                                a, j = _read_varint(v4, j)
                                entry["found"].append(a)
                results.append(entry)
    return results


# --------------------------------------------------------------------------- #
# Injector                                                                    #
# --------------------------------------------------------------------------- #
class WsI2cInjectError(RuntimeError):
    """The I2C probe injection could not be completed."""


class WsI2cProbeInjector:
    """Drive a v2 I2C Probe/Add at a checked-in DUT through the protomq broker."""

    def __init__(
        self,
        *,
        broker_host: str,
        mqtt_port: int = 1884,
        api_url: str | None = None,
        io_username: str = "hil",
    ) -> None:
        self.broker_host = broker_host
        self.mqtt_port = mqtt_port
        self.api_url = (api_url or f"http://{broker_host}:5173").rstrip("/")
        self.io_username = io_username

    def b2d_topic(self, uid: str) -> str:
        return f"{self.io_username}/ws-b2d/{uid}"

    def d2b_topic(self, uid: str) -> str:
        return f"{self.io_username}/ws-d2b/{uid}"

    @staticmethod
    def _uid_from_topic(topic: str) -> str | None:
        """Pull ``<uid>`` out of ``<user>/ws-d2b/<uid>`` or ``<user>/ws-b2d/<uid>``."""
        for sep in ("/ws-d2b/", "/ws-b2d/"):
            if sep in topic:
                uid = topic.split(sep, 1)[1].split("/", 1)[0]
                return uid or None
        return None

    async def wait_for_checkin(self, *, timeout: float = 120.0, settle_s: float = 2.0) -> str | None:
        """Subscribe to ``#`` and return the device_uid once the DUT is on the bus.

        v2 has no per-component "pinConfigComplete"; the device publishing on its
        ``ws-d2b`` topic (its checkin, or the broker's ``ws-b2d`` response) is the
        signal it's connected. After learning the uid we wait ``settle_s`` for the
        checkin handshake to finish before the caller injects."""
        if not _AIOMQTT_AVAILABLE:
            raise WsI2cInjectError("aiomqtt not installed; cannot observe checkin")
        deadline = asyncio.get_event_loop().time() + timeout
        try:
            async with aiomqtt.Client(hostname=self.broker_host, port=self.mqtt_port) as client:
                await client.subscribe("#")
                messages = aiter(client.messages)
                while asyncio.get_event_loop().time() < deadline:
                    remaining = deadline - asyncio.get_event_loop().time()
                    try:
                        message = await asyncio.wait_for(anext(messages), timeout=remaining)
                    except (TimeoutError, StopAsyncIteration):
                        break
                    uid = self._uid_from_topic(str(message.topic))
                    if uid and uid != "info":
                        if settle_s > 0:
                            await asyncio.sleep(settle_s)
                        return uid
        except WsI2cInjectError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("wait_for_checkin error: %s", exc)
        return None

    async def _echo(self, topic: str, payload: bytes) -> dict[str, Any]:
        """Publish *payload* to *topic* NOW via protomq ``POST /api/echo``.

        protomq does ``Buffer.from(payload, 'latin1')`` so the binary protobuf is
        sent as a latin1 string (same convention as the v1 injector)."""
        body = {"topic": topic, "payload": payload.decode("latin1")}
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.post(f"{self.api_url}/api/echo", json=body)
            r.raise_for_status()
            return r.json()

    async def add_mux(
        self, uid: str, *, mux_address: int, pin_scl: int, pin_sda: int, name: str = "pca9548"
    ) -> dict[str, Any]:
        """Register the MUX on the DUT's bus (required before any channel probe)."""
        payload = build_add_mux(
            pin_scl=pin_scl, pin_sda=pin_sda, mux_address=mux_address, name=name
        )
        topic = self.b2d_topic(uid)
        resp = await self._echo(topic, payload)
        return {"topic": topic, "payload_hex": payload.hex(" "), "echo_response": resp}

    async def probe(
        self,
        uid: str,
        *,
        pin_scl: int,
        pin_sda: int,
        addresses: list[int] | None = None,
        mux_address: int = 0,
        mux_channel: int = 0,
        observe_s: float = 15.0,
    ) -> dict[str, Any]:
        """Fire one Probe (bare bus if ``mux_address==0``, else that channel) and
        capture the Probed reply on ``ws-d2b/<uid>``.

        Returns ``{"found": [addr...], "channel", "payload_hex", "raw": [...]}``.
        Subscribes BEFORE publishing so the reply is never missed."""
        addrs = list(addresses) if addresses is not None else list(DEFAULT_PROBE_ADDRESSES)
        payload = build_probe(
            pin_scl=pin_scl,
            pin_sda=pin_sda,
            addresses=addrs,
            mux_address=mux_address,
            mux_channel=mux_channel,
        )
        if not _AIOMQTT_AVAILABLE:
            raise WsI2cInjectError("aiomqtt not installed; cannot capture probed reply")
        d2b = self.d2b_topic(uid)
        found: list[int] = []
        raw: list[dict[str, Any]] = []
        deadline = asyncio.get_event_loop().time() + observe_s
        async with aiomqtt.Client(hostname=self.broker_host, port=self.mqtt_port) as client:
            await client.subscribe(d2b)
            # Publish only after the subscription is live.
            await self._echo(self.b2d_topic(uid), payload)
            messages = aiter(client.messages)
            while asyncio.get_event_loop().time() < deadline:
                remaining = deadline - asyncio.get_event_loop().time()
                try:
                    message = await asyncio.wait_for(anext(messages), timeout=remaining)
                except (TimeoutError, StopAsyncIteration):
                    break
                payload_in = message.payload
                if not isinstance(payload_in, bytes):
                    continue
                try:
                    entries = parse_probed(payload_in)
                except Exception as exc:  # noqa: BLE001 - tolerate non-probed D2B traffic
                    log.debug("parse_probed skip: %s", exc)
                    continue
                if not entries:
                    continue
                # Keep the result matching the channel we asked for (mux) or any
                # bare-bus result (mux_address==0).
                for e in entries:
                    raw.append(e)
                    if mux_address == 0:
                        if not e.get("mux_address"):
                            found = e["found"]
                    elif e.get("mux_channel") == mux_channel:
                        found = e["found"]
                if found:
                    break
        return {
            "found": sorted(set(found)),
            "channel": mux_channel if mux_address else None,
            "payload_hex": payload.hex(" "),
            "raw": raw,
        }
