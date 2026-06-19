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
