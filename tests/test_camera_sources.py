"""Tests for adapters.camera.sources — mocked httpx, no real camera."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_ip_camera_fetches_url():
    from hil_controller.adapters.camera.sources import IPCamera

    cam = IPCamera("http://192.168.1.249:8080/shot.jpg")

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.content = b"\xff\xd8\xff\xe0"

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with (
        patch("hil_controller.adapters.camera.sources._NUMPY", True),
        patch("hil_controller.adapters.camera.sources._CV2", False),
        patch("hil_controller.adapters.camera.sources.np") as mock_np,
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        mock_np.frombuffer = MagicMock(return_value=b"\xff\xd8")
        await cam.read_frame()

    mock_client.get.assert_called_once_with("http://192.168.1.249:8080/shot.jpg")


@pytest.mark.asyncio
async def test_ip_camera_returns_none_on_error():
    from hil_controller.adapters.camera.sources import IPCamera

    cam = IPCamera("http://bad-host/shot.jpg", timeout=1.0)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=Exception("connection refused"))

    with (
        patch("hil_controller.adapters.camera.sources._NUMPY", True),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        result = await cam.read_frame()

    assert result is None


@pytest.mark.asyncio
async def test_ip_camera_close_is_noop():
    from hil_controller.adapters.camera.sources import IPCamera

    cam = IPCamera("http://example.com/shot.jpg")
    await cam.close()  # must not raise


def test_ip_camera_url_stored():
    from hil_controller.adapters.camera.sources import IPCamera

    cam = IPCamera("http://192.168.1.249:8080/shot.jpg")
    assert cam.url == "http://192.168.1.249:8080/shot.jpg"


def test_ip_camera_default_timeout():
    from hil_controller.adapters.camera.sources import IPCamera

    cam = IPCamera("http://example.com/shot.jpg")
    assert cam.timeout == 5.0


@pytest.mark.asyncio
async def test_v4l2_camera_read_frame_not_implemented():
    from hil_controller.adapters.camera.sources import V4L2Camera

    cam = V4L2Camera(transport=None, device_index=0)
    with pytest.raises(NotImplementedError):
        await cam.read_frame()


@pytest.mark.asyncio
async def test_v4l2_camera_close_noop():
    from hil_controller.adapters.camera.sources import V4L2Camera

    cam = V4L2Camera(transport=None)
    await cam.close()
