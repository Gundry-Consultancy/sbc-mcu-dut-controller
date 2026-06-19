"""Tests for the UPnP IGD port-mapping helper."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

# upnp.py imports miniupnpc at module load; it's an optional dep not installed
# in CI / dev. Skip the whole module cleanly instead of erroring at collection.
upnp = pytest.importorskip(
    "hil_controller.upnp",
    reason="miniupnpc not installed (optional UPnP dependency)",
)

# miniupnpc getspecificportmapping returns:
#   (internalIP, internalPort, desc, enabled, remoteHost, leaseDuration)
# or None if no mapping exists.
_OUR_IP = "192.168.1.100"
_OTHER_IP = "192.168.1.50"
_IGD_URL = "http://192.168.1.1:49152/abc/gatedesc.xml"
_MAPPING_OURS = (_OUR_IP, 8080, "hil-controller", True, "", 3600)
_MAPPING_OTHER = (_OTHER_IP, 8080, "other-service", True, "", 3600)


def _make_upnp_mock(
    discover_count: int = 1,
    map_result: bool = True,
    existing: tuple | None = None,
) -> MagicMock:
    m = MagicMock()
    m.discover.return_value = discover_count
    m.selectigd.return_value = _IGD_URL
    m.externalipaddress.return_value = "1.2.3.4"
    m.getspecificportmapping.return_value = existing
    m.addportmapping.return_value = map_result
    m.deleteportmapping.return_value = True
    return m


@pytest.mark.asyncio
async def test_open_port_maps_successfully():
    mock_upnp = _make_upnp_mock()
    with (
        patch("miniupnpc.UPnP", return_value=mock_upnp),
        patch("hil_controller.upnp.local_ip_toward", return_value=_OUR_IP),
    ):
        result = await upnp.open_port(8080, 8080, lease_seconds=3600)

    assert result == _OUR_IP
    mock_upnp.selectigd.assert_called_once()
    mock_upnp.addportmapping.assert_called_once_with(
        8080, "TCP", _OUR_IP, 8080, upnp._DESCRIPTION, 3600
    )


@pytest.mark.asyncio
async def test_open_port_no_igd_returns_none():
    mock_upnp = _make_upnp_mock(discover_count=0)
    with (
        patch("miniupnpc.UPnP", return_value=mock_upnp),
        patch("hil_controller.upnp.local_ip_toward", return_value=_OUR_IP),
    ):
        result = await upnp.open_port(8080, 8080)

    assert result is None
    mock_upnp.addportmapping.assert_not_called()


@pytest.mark.asyncio
async def test_open_port_already_mapped_to_us_skips_add():
    mock_upnp = _make_upnp_mock(existing=_MAPPING_OURS)
    with (
        patch("miniupnpc.UPnP", return_value=mock_upnp),
        patch("hil_controller.upnp.local_ip_toward", return_value=_OUR_IP),
    ):
        result = await upnp.open_port(8080, 8080)

    assert result == _OUR_IP
    mock_upnp.deleteportmapping.assert_not_called()
    mock_upnp.addportmapping.assert_not_called()


@pytest.mark.asyncio
async def test_open_port_mapped_to_other_host_reclaims():
    mock_upnp = _make_upnp_mock(existing=_MAPPING_OTHER)
    with (
        patch("miniupnpc.UPnP", return_value=mock_upnp),
        patch("hil_controller.upnp.local_ip_toward", return_value=_OUR_IP),
    ):
        result = await upnp.open_port(8080, 8080, lease_seconds=3600)

    assert result == _OUR_IP
    mock_upnp.deleteportmapping.assert_called_once_with(8080, "TCP")
    mock_upnp.addportmapping.assert_called_once_with(
        8080, "TCP", _OUR_IP, 8080, upnp._DESCRIPTION, 3600
    )
    # delete must precede add
    assert mock_upnp.method_calls.index(
        call.deleteportmapping(8080, "TCP")
    ) < mock_upnp.method_calls.index(
        call.addportmapping(8080, "TCP", _OUR_IP, 8080, upnp._DESCRIPTION, 3600)
    )


@pytest.mark.asyncio
async def test_open_port_add_failure_returns_none():
    mock_upnp = _make_upnp_mock(map_result=False)
    with (
        patch("miniupnpc.UPnP", return_value=mock_upnp),
        patch("hil_controller.upnp.local_ip_toward", return_value=_OUR_IP),
    ):
        result = await upnp.open_port(8080, 8080)

    assert result is None


@pytest.mark.asyncio
async def test_close_port_removes_mapping():
    mock_upnp = _make_upnp_mock()
    with patch("miniupnpc.UPnP", return_value=mock_upnp):
        await upnp.close_port(8080)

    mock_upnp.deleteportmapping.assert_called_once_with(8080, "TCP")


@pytest.mark.asyncio
async def test_close_port_no_igd_is_silent():
    mock_upnp = _make_upnp_mock(discover_count=0)
    with patch("miniupnpc.UPnP", return_value=mock_upnp):
        await upnp.close_port(8080)  # must not raise

    mock_upnp.deleteportmapping.assert_not_called()


def test_local_ip_toward_returns_string():
    # Smoke test: routing table must resolve something for a public IP.
    ip = upnp.local_ip_toward("8.8.8.8")
    assert isinstance(ip, str)
    parts = ip.split(".")
    assert len(parts) == 4
