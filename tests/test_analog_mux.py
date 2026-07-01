"""Tests for AnalogMuxAdapter — the HTTP client for the analog strand-mux box."""

import httpx
import pytest

from hil_controller.adapters.analog_mux import AnalogMuxAdapter, AnalogMuxError


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_select_hits_channel_endpoint():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"active": "dut-01", "group": "muxA", "channel": 2})

    client = _client(handler)
    mux = AnalogMuxAdapter("http://mux:8080/", token="sekret", client=client)
    result = await mux.select("muxA", 2)
    await client.aclose()

    assert seen["method"] == "POST"
    assert seen["url"] == "http://mux:8080/api/groups/muxA/select/2"
    assert seen["auth"] == "Bearer sekret"
    assert result["active"] == "dut-01"


async def test_isolate_hits_isolate_endpoint():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"active": None})

    client = _client(handler)
    mux = AnalogMuxAdapter("http://mux:8080", client=client)
    await mux.isolate()
    await client.aclose()
    assert seen["url"] == "http://mux:8080/api/isolate"


async def test_no_token_sends_no_auth_header():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={})

    client = _client(handler)
    await AnalogMuxAdapter("http://mux:8080", client=client).select("muxA", 0)
    await client.aclose()
    assert seen["auth"] is None


async def test_http_error_raises_analogmuxerror():
    def handler(request):
        return httpx.Response(503, json={"error": "control bus unavailable"})

    client = _client(handler)
    mux = AnalogMuxAdapter("http://mux:8080", client=client)
    with pytest.raises(AnalogMuxError):
        await mux.select("muxA", 0)
    await client.aclose()
