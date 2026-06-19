"""Camera source abstractions: IPCamera (HTTP) and V4L2Camera (SSH, Phase 2 stub)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

try:
    import numpy as np

    _NUMPY = True
except ImportError:
    _NUMPY = False

try:
    import cv2

    _CV2 = True
except ImportError:
    _CV2 = False


@runtime_checkable
class CameraSource(Protocol):
    """Protocol for reading frames from a camera."""

    async def read_frame(self) -> np.ndarray | None: ...
    async def read_frames(self) -> AsyncIterator[np.ndarray]: ...
    async def close(self) -> None: ...


class IPCamera:
    """Read frames from an HTTP camera endpoint (snapshot JPEG or MJPEG stream).

    For snapshot URLs (e.g. ``/shot.jpg``) fetches a single JPEG via httpx.
    ``cv2.imdecode`` decodes to a BGR ndarray when cv2 is available.
    """

    def __init__(self, url: str, timeout: float = 5.0) -> None:
        self.url = url
        self.timeout = timeout

    async def read_frame(self) -> np.ndarray | None:
        if not _NUMPY:
            return None
        try:
            import httpx

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.get(self.url)
                r.raise_for_status()
                data = r.content
            arr = np.frombuffer(data, dtype=np.uint8)
            if _CV2:
                return cv2.imdecode(arr, cv2.IMREAD_COLOR)
            return arr
        except Exception as exc:
            logger.warning("IPCamera.read_frame failed (%s): %s", self.url, exc)
            return None

    async def read_frames(self) -> AsyncIterator[np.ndarray]:
        while True:
            frame = await self.read_frame()
            if frame is not None:
                yield frame

    async def close(self) -> None:
        pass


class V4L2Camera:
    """Read frames from a v4l2 device on a remote HIL host via SSH.

    Phase 2 stub — requires HostTransport wiring (not yet implemented).
    """

    def __init__(self, transport: object, device_index: int = 0) -> None:
        self.transport = transport
        self.device_index = device_index

    async def read_frame(self) -> np.ndarray | None:
        raise NotImplementedError("V4L2Camera.read_frame requires Phase 2 SSH transport wiring")

    async def read_frames(self) -> AsyncIterator[np.ndarray]:
        raise NotImplementedError("V4L2Camera.read_frames requires Phase 2 SSH transport wiring")
        yield  # make this an async generator

    async def close(self) -> None:
        pass
