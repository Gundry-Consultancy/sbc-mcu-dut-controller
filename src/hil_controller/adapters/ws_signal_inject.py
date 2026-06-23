"""Inject a v1 WipperSnapper signal (pixelWrite) via the protomq broker.

Built for the pixelWrite-to-uninitialised-strand regression: a v1
``signal.v1.PixelsRequest{ req_pixels_write: PixelsWriteRequest{ type=NEOPIXEL,
pin="D0", color=200 } }`` sent to a freshly-checked-in device CRASHES release
1.0.0-beta.127 (null-deref: pin ``D0``→0 collides with the zero-init strand
sentinel, ``getStrandIdx`` false-matches an uninitialised strand, then
``neoPixelPtr->fill()`` on ``nullptr`` → panic/reboot) but is handled gracefully
by the #927 fix (in beta.129+), which hits the ``ERROR: Pixel strand not found``
guard and continues without resetting.

Injection uses protomq's own HTTP API rather than touching ``vendor/protomq``:

* ``POST /api/echo {topic, payload}`` — publish a raw protobuf payload to a
  topic NOW (the "fire when ready / checked in" command). Primary path.
* ``POST /api/autoresponse {trigger, match, response}`` — queue a B2D response
  that fires when a matching device message arrives (the "queue it up" path).
  Best-effort alternative (V2-shaped); the echo path is authoritative for v1.

Checkin + the crash/survive verdict are observed over MQTT (aiomqtt): we learn
the device_uid from the ``<user>/wprsnpr/<uid>/...`` topics it publishes, treat
``signals/device/pinConfigComplete`` as "ready", then after firing watch for the
device RE-REGISTERING (a fresh checkin) within a short window — that re-checkin
is the reboot/crash signal, with no serial-reader contention.
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


# v1 enum wippersnapper.pixels.v1.PixelsType
PIXELS_TYPE_NEOPIXEL = 1
PIXELS_TYPE_DOTSTAR = 2

# Topic fragments (mirror the firmware string constants in Wippersnapper.h).
_TOPIC_PIXELS_B2D = "signals/broker/pixel"  # broker -> device pixel write
_TOPIC_PINCFG_DONE = "signals/device/pinConfigComplete"  # device "ready"


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


def encode_pixels_write(
    pin: str = "D0", color: int = 200, pixels_type: int = PIXELS_TYPE_NEOPIXEL
) -> bytes:
    """Serialize a v1 ``signal.v1.PixelsRequest`` carrying a ``PixelsWriteRequest``.

    Wire layout (nanopb, raw bytes — exactly what the device's subscription
    callback decodes):

    * ``PixelsWriteRequest`` = f1 varint ``pixels_type`` | f2 string
      ``pixels_pin_data`` | f3 varint ``pixels_color``.
    * ``PixelsRequest`` = f3 (``req_pixels_write``) length-delimited message.

    For (``D0``, 200, NEOPIXEL) this is the 11 bytes
    ``1a 09 08 01 12 02 44 30 18 c8 01``.
    """
    pin_bytes = pin.encode("ascii")
    inner = bytearray()
    inner += b"\x08" + _varint(pixels_type)  # field 1: pixels_type
    inner += b"\x12" + _varint(len(pin_bytes)) + pin_bytes  # field 2: pixels_pin_data
    inner += b"\x18" + _varint(color)  # field 3: pixels_color
    # PixelsRequest field 3 (req_pixels_write), wire type 2 (len-delimited) → tag 0x1A
    return b"\x1a" + _varint(len(inner)) + bytes(inner)


def encode_i2c_scan_request(*, port: int = 0, scl: int, sda: int, freq: int = 100000) -> bytes:
    """Serialize a v1 ``signal.v1.I2CRequest{ req_i2c_scan: I2CBusScanRequest }``.

    Drives the known-good v1 firmware's I2C bus scan, explicitly selecting the
    TwoWire instance via ``i2c_port_number`` (+ a ``bus_init_request`` carrying
    the pins/freq/port) so we can prove which port reaches the STEMMA sensors.

    Wire layout (firmware ``wippersnapper/i2c/v1/i2c.pb.h``):
    * ``I2CBusInitRequest`` = f1 ``i2c_pin_scl`` | f2 ``i2c_pin_sda`` |
      f3 ``i2c_frequency`` | f4 ``i2c_port_number`` (all varint).
    * ``I2CBusScanRequest`` = f1 ``i2c_port_number`` | f2 ``bus_init_request`` (msg).
    * ``I2CRequest`` = f2 (``req_i2c_scan``) length-delimited message.
    """
    bus_init = (
        b"\x08" + _varint(scl)   # f1 i2c_pin_scl
        + b"\x10" + _varint(sda)  # f2 i2c_pin_sda
        + b"\x18" + _varint(freq)  # f3 i2c_frequency
        + b"\x20" + _varint(port)  # f4 i2c_port_number
    )
    scan_req = (
        b"\x08" + _varint(port)  # f1 i2c_port_number
        + b"\x12" + _varint(len(bus_init)) + bus_init  # f2 bus_init_request
    )
    # I2CRequest field 2 (req_i2c_scan), wiretype 2 → tag 0x12
    return b"\x12" + _varint(len(scan_req)) + scan_req


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


def decode_i2c_scan_response(data: bytes) -> list[int]:
    """Decode a v1 ``signal.v1.I2CResponse`` → list of found 7-bit addresses.

    ``I2CResponse`` f2 (``resp_i2c_scan``) → ``I2CBusScanResponse`` f1
    (``addresses_found``, repeated uint32; nanopb emits it packed). Tolerates
    packed and unpacked. Returns ``[]`` if the message isn't a scan response."""

    def walk(buf):
        out = []
        i = 0
        while i < len(buf):
            key, i = _read_varint(buf, i)
            f, wt = key >> 3, key & 7
            if wt == 0:
                v, i = _read_varint(buf, i)
                out.append((f, 0, v))
            elif wt == 2:
                ln, i = _read_varint(buf, i)
                out.append((f, 2, buf[i : i + ln]))
                i += ln
            elif wt == 5:
                out.append((f, 5, buf[i : i + 4]))
                i += 4
            elif wt == 1:
                out.append((f, 1, buf[i : i + 8]))
                i += 8
            else:
                raise ValueError(f"bad wiretype {wt}")
        return out

    found: list[int] = []
    for f, wt, v in walk(data):
        if f == 2 and wt == 2:  # resp_i2c_scan -> I2CBusScanResponse
            for f2, wt2, v2 in walk(v):
                if f2 == 1:  # addresses_found
                    if wt2 == 2:  # packed
                        j = 0
                        while j < len(v2):
                            a, j = _read_varint(v2, j)
                            found.append(a)
                    elif wt2 == 0:  # unpacked
                        found.append(v2)
    return found


class WsInjectError(RuntimeError):
    """The signal injection could not be completed."""


class WsSignalInjector:
    """Drive a v1 pixelWrite at a checked-in DUT through the protomq broker."""

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
        self._prefix = f"{io_username}/wprsnpr/"

    def pixel_topic(self, device_uid: str) -> str:
        """The broker→device pixel-write topic for *device_uid*."""
        return f"{self._prefix}{device_uid}/{_TOPIC_PIXELS_B2D}"

    def _uid_from_topic(self, topic: str) -> str | None:
        """Pull ``<device_uid>`` out of ``<user>/wprsnpr/<uid>/...``."""
        if not topic.startswith(self._prefix):
            return None
        rest = topic[len(self._prefix) :]
        uid = rest.split("/", 1)[0]
        # "info" is the registration namespace (<user>/wprsnpr/info/status), not a uid.
        if not uid or uid == "info":
            return None
        return uid

    async def wait_for_checkin(
        self, *, timeout: float = 120.0, settle_s: float = 1.0
    ) -> str | None:
        """Subscribe to ``<user>/wprsnpr/#`` and return the device_uid once ready.

        "Ready" = we've seen the device publish ``pinConfigComplete`` (it has
        finished registration and the main loop will process signals). Falls
        back to: learned a uid and ``settle_s`` elapsed with no further config
        traffic. Returns ``None`` on timeout (caller decides whether to abort).
        """
        if not _AIOMQTT_AVAILABLE:
            raise WsInjectError("aiomqtt not installed; cannot observe checkin")
        deadline = asyncio.get_event_loop().time() + timeout
        learned_uid: str | None = None
        try:
            async with aiomqtt.Client(hostname=self.broker_host, port=self.mqtt_port) as client:
                await client.subscribe(f"{self._prefix}#")
                messages = aiter(client.messages)
                while asyncio.get_event_loop().time() < deadline:
                    remaining = deadline - asyncio.get_event_loop().time()
                    try:
                        message = await asyncio.wait_for(anext(messages), timeout=remaining)
                    except (TimeoutError, StopAsyncIteration):
                        break
                    topic = str(message.topic)
                    uid = self._uid_from_topic(topic)
                    if uid:
                        learned_uid = uid
                        if topic.endswith(_TOPIC_PINCFG_DONE):
                            if settle_s > 0:
                                await asyncio.sleep(settle_s)
                            return uid
        except WsInjectError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("wait_for_checkin error: %s", exc)
        return learned_uid

    async def fire_pixel_write(
        self,
        device_uid: str,
        *,
        pin: str = "D0",
        color: int = 200,
        pixels_type: int = PIXELS_TYPE_NEOPIXEL,
    ) -> dict[str, Any]:
        """Publish the v1 pixelWrite NOW via protomq ``POST /api/echo``.

        Returns a small record (topic + payload hex + protomq response) suitable
        for the flash.log transcript. The payload is sent as a latin1 string
        because protomq does ``Buffer.from(payload, 'latin1')``.
        """
        payload = encode_pixels_write(pin=pin, color=color, pixels_type=pixels_type)
        topic = self.pixel_topic(device_uid)
        body = {"topic": topic, "payload": payload.decode("latin1")}
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.post(f"{self.api_url}/api/echo", json=body)
            r.raise_for_status()
            resp = r.json()
        return {"topic": topic, "payload_hex": payload.hex(" "), "echo_response": resp}

    async def register_autoresponder(
        self,
        *,
        name: str = "hil-pixelwrite",
        trigger: str = "checkin.request",
        response: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Queue a B2D autoresponse that fires on *trigger* (best-effort, V2-shaped).

        The echo path is authoritative for a v1 device; this is the "queue it
        up" alternative for flexibility. ``response`` must be a BrokerToDevice-
        shaped object; protomq validates it at registration.
        """
        if response is None:
            raise WsInjectError("register_autoresponder needs a BrokerToDevice-shaped 'response'")
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.post(
                f"{self.api_url}/api/autoresponse",
                json={"name": name, "trigger": trigger, "response": response},
            )
            r.raise_for_status()
            return r.json()

    async def observe_reboot(self, device_uid: str, *, timeout: float = 12.0) -> bool:
        """After firing, return True if the device RE-CHECKS-IN within *timeout*.

        A fresh registration/pinConfigComplete from the same uid after the
        pixelWrite means the firmware crashed and rebooted (release 1.0.0-beta.127);
        silence means it survived and continued (the #927 fix). Pure MQTT — does
        not touch the serial port the capture stage owns.
        """
        if not _AIOMQTT_AVAILABLE:
            raise WsInjectError("aiomqtt not installed; cannot observe reboot")
        deadline = asyncio.get_event_loop().time() + timeout
        try:
            async with aiomqtt.Client(hostname=self.broker_host, port=self.mqtt_port) as client:
                await client.subscribe(f"{self._prefix}#")
                messages = aiter(client.messages)
                while asyncio.get_event_loop().time() < deadline:
                    remaining = deadline - asyncio.get_event_loop().time()
                    try:
                        message = await asyncio.wait_for(anext(messages), timeout=remaining)
                    except (TimeoutError, StopAsyncIteration):
                        break
                    topic = str(message.topic)
                    if self._uid_from_topic(topic) == device_uid and (
                        topic.endswith(_TOPIC_PINCFG_DONE) or topic.endswith("info/status")
                    ):
                        return True  # device re-registered → it rebooted
        except WsInjectError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("observe_reboot error: %s", exc)
        return False

    # ---- v1 I2C bus scan (Wire0/Wire1 reachability test on known-good v1) ----
    def i2c_broker_topic(self, device_uid: str) -> str:
        return f"{self._prefix}{device_uid}/signals/broker/i2c"

    def i2c_device_topic(self, device_uid: str) -> str:
        return f"{self._prefix}{device_uid}/signals/device/i2c"

    async def i2c_scan(
        self,
        device_uid: str,
        *,
        port: int = 0,
        scl: int,
        sda: int,
        freq: int = 100000,
        observe_s: float = 15.0,
    ) -> dict[str, Any]:
        """Fire a v1 I2C bus scan on *port* (pins scl/sda) and capture the reply.

        Subscribes to the device's i2c response topic BEFORE publishing the
        request via ``POST /api/echo`` to the broker i2c topic, then decodes the
        ``I2CBusScanResponse``. Returns found addresses + the wire payloads."""
        if not _AIOMQTT_AVAILABLE:
            raise WsInjectError("aiomqtt not installed; cannot capture i2c scan reply")
        payload = encode_i2c_scan_request(port=port, scl=scl, sda=sda, freq=freq)
        dev_topic = self.i2c_device_topic(device_uid)
        brkr_topic = self.i2c_broker_topic(device_uid)
        found: list[int] = []
        raw_hex = ""
        deadline = asyncio.get_event_loop().time() + observe_s
        async with aiomqtt.Client(hostname=self.broker_host, port=self.mqtt_port) as client:
            await client.subscribe(dev_topic)
            body = {"topic": brkr_topic, "payload": payload.decode("latin1")}
            async with httpx.AsyncClient(timeout=10) as http:
                r = await http.post(f"{self.api_url}/api/echo", json=body)
                r.raise_for_status()
            messages = aiter(client.messages)
            while asyncio.get_event_loop().time() < deadline:
                remaining = deadline - asyncio.get_event_loop().time()
                try:
                    message = await asyncio.wait_for(anext(messages), timeout=remaining)
                except (TimeoutError, StopAsyncIteration):
                    break
                data = message.payload
                if not isinstance(data, bytes):
                    continue
                try:
                    addrs = decode_i2c_scan_response(data)
                except Exception as exc:  # noqa: BLE001
                    log.debug("decode_i2c_scan_response skip: %s", exc)
                    continue
                if addrs:
                    found = addrs
                    raw_hex = data.hex(" ")
                    break
                raw_hex = data.hex(" ")  # a scan response with no devices still arrives
                break
        return {
            "found": sorted(set(found)),
            "port": port,
            "payload_hex": payload.hex(" "),
            "response_hex": raw_hex,
        }
