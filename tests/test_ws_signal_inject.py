"""Tests for the v1 WipperSnapper pixelWrite injector."""

from __future__ import annotations

import pytest

import hil_controller.adapters.ws_signal_inject as mod
from hil_controller.adapters.ws_signal_inject import (
    PIXELS_TYPE_DOTSTAR,
    WsSignalInjector,
    _varint,
    encode_pixels_write,
)


def test_varint_boundaries() -> None:
    assert _varint(0) == b"\x00"
    assert _varint(127) == b"\x7f"
    assert _varint(128) == b"\x80\x01"
    assert _varint(200) == b"\xc8\x01"


def test_encode_pixels_write_d0_200_exact_bytes() -> None:
    # signal.v1.PixelsRequest{ req_pixels_write: PixelsWriteRequest{
    #   pixels_type=NEOPIXEL(1), pixels_pin_data="D0", pixels_color=200 } }
    assert encode_pixels_write("D0", 200).hex(" ") == "1a 09 08 01 12 02 44 30 18 c8 01"


def test_encode_pixels_write_other_values() -> None:
    p = encode_pixels_write("A1", 1, pixels_type=PIXELS_TYPE_DOTSTAR)
    assert p[0] == 0x1A  # PixelsRequest field 3, length-delimited
    inner = p[2:]
    assert inner[0:2] == b"\x08\x02"  # field1 pixels_type=2 (DOTSTAR)
    assert inner[2:6] == b"\x12\x02A1"  # field2 pixels_pin_data="A1"
    assert inner[6:8] == b"\x18\x01"  # field3 pixels_color=1


def test_pixel_topic_and_uid_parse() -> None:
    inj = WsSignalInjector(broker_host="h", io_username="hil")
    assert (
        inj.pixel_topic("io-wipper-qtpyABC") == "hil/wprsnpr/io-wipper-qtpyABC/signals/broker/pixel"
    )
    assert (
        inj._uid_from_topic("hil/wprsnpr/io-wipper-qtpyABC/signals/device/pinConfigComplete")
        == "io-wipper-qtpyABC"
    )
    assert inj._uid_from_topic("hil/wprsnpr/info/status") is None  # registration ns, not a uid
    assert inj._uid_from_topic("other/topic") is None


def test_api_url_default_and_override() -> None:
    assert WsSignalInjector(broker_host="bench").api_url == "http://bench:5173"
    assert WsSignalInjector(broker_host="bench", api_url="http://x:9/").api_url == "http://x:9"


@pytest.mark.asyncio
async def test_fire_pixel_write_posts_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class _Resp:
        def raise_for_status(self) -> None:  # noqa: D401
            return None

        def json(self) -> dict:
            return {"status": "OK"}

    class _Client:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *a) -> bool:
            return False

        async def post(self, url, json=None):
            captured["url"] = url
            captured["json"] = json
            return _Resp()

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    inj = WsSignalInjector(broker_host="h", io_username="hil")
    rec = await inj.fire_pixel_write("io-wipper-x", pin="D0", color=200)

    assert captured["url"].endswith("/api/echo")
    assert captured["json"]["topic"] == "hil/wprsnpr/io-wipper-x/signals/broker/pixel"
    # protomq does Buffer.from(payload, 'latin1') — the bytes must round-trip via latin1.
    assert (
        captured["json"]["payload"].encode("latin1").hex(" ") == "1a 09 08 01 12 02 44 30 18 c8 01"
    )
    assert rec["payload_hex"] == "1a 09 08 01 12 02 44 30 18 c8 01"
    assert rec["topic"].endswith("/signals/broker/pixel")
