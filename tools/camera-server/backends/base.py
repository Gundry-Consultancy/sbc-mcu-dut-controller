"""Backend interface and registry.

A Backend owns the camera handle for the lifetime of the server. It runs a
background grabber that keeps a single-slot ``latest_frame`` warm, so the
HTTP handlers can hand out fresh JPEGs without paying the open/AF cost
per request.
"""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass


class BackendUnavailable(RuntimeError):
    """Raised when a backend's dependencies or hardware are missing."""


@dataclass
class FrameConfig:
    width: int = 1280
    height: int = 720
    fps: int = 10
    jpeg_quality: int = 85


class Backend(ABC):
    """Abstract camera backend.

    Concrete backends spin up a grabber thread in ``start()`` that writes
    JPEG bytes into ``self._latest`` under ``self._lock``. ``read_jpeg()``
    returns a copy of whatever is currently in the slot.
    """

    name: str = "abstract"

    def __init__(self, cfg: FrameConfig):
        self.cfg = cfg
        self._latest: bytes | None = None
        self._latest_ts: float = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @abstractmethod
    def _open(self) -> None:
        """Open the camera handle. Raise BackendUnavailable if impossible."""

    @abstractmethod
    def _grab_jpeg(self) -> bytes:
        """Grab one frame and return it encoded as JPEG bytes."""

    @abstractmethod
    def _close(self) -> None:
        """Release the camera handle."""

    def supports_autofocus(self) -> bool:
        return False

    def set_lens(self, *, mode: str, position: float | None = None) -> None:
        """Switch between continuous AF and a fixed lens position.

        ``mode`` is ``"auto"`` (continuous AF) or ``"manual"``. When manual,
        ``position`` is a dioptre value in the sensor-specific range.
        Default no-op; subclasses override.
        """
        raise NotImplementedError(f"{self.name} does not support lens control")

    def get_lens(self) -> dict:
        """Return the current lens mode and reported position."""
        return {"mode": "unknown", "position": None}

    def start(self) -> None:
        self._open()
        self._thread = threading.Thread(
            target=self._run, name=f"{self.name}-grabber", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._close()

    def capture_full_jpeg(self) -> bytes:
        """Capture a one-shot snapshot at sensor-native resolution.

        Default: not implemented (caller falls back to ``read_jpeg``).
        Backends that can reconfigure to a still-capture mode for max
        resolution should override.
        """
        raise NotImplementedError(f"{self.name} does not support full-res capture")

    def read_jpeg(self, max_age: float = 2.0) -> bytes:
        """Return the latest JPEG. Blocks briefly for the first frame."""
        deadline = time.monotonic() + max_age
        while time.monotonic() < deadline:
            with self._lock:
                if self._latest is not None and (
                    time.monotonic() - self._latest_ts
                ) < max_age:
                    return self._latest
            time.sleep(0.02)
        raise TimeoutError(f"{self.name}: no frame within {max_age}s")

    def _run(self) -> None:
        interval = 1.0 / max(self.cfg.fps, 1)
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                jpeg = self._grab_jpeg()
                with self._lock:
                    self._latest = jpeg
                    self._latest_ts = time.monotonic()
            except Exception as exc:  # noqa: BLE001 — backend-level isolation
                # Don't kill the thread on transient errors; let the next
                # tick retry. Stash the error string for /health to surface.
                self._last_error = f"{type(exc).__name__}: {exc}"
                time.sleep(0.5)
                continue
            elapsed = time.monotonic() - t0
            if elapsed < interval:
                time.sleep(interval - elapsed)


# ---- registry -------------------------------------------------------------

_BACKENDS: dict[str, str] = {
    # name -> module path (relative to this package)
    "picamera2": ".picamera2_backend",
    "v4l2": ".v4l2_backend",
}


def load_backend(name: str, cfg: FrameConfig, **kwargs) -> Backend:
    """Import and instantiate a backend by name."""
    import importlib

    if name not in _BACKENDS:
        raise BackendUnavailable(f"unknown backend: {name}")
    module = importlib.import_module(_BACKENDS[name], package=__package__)
    cls = module.BACKEND_CLASS
    return cls(cfg, **kwargs)


def autodetect(cfg: FrameConfig, **kwargs) -> Backend:
    """Try each registered backend in order, return the first that opens."""
    errors: list[str] = []
    for name in _BACKENDS:
        try:
            backend = load_backend(name, cfg, **kwargs)
            backend._open()
            backend._close()
            return load_backend(name, cfg, **kwargs)
        except BackendUnavailable as exc:
            errors.append(f"{name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    raise BackendUnavailable(
        "no working backend found:\n  " + "\n  ".join(errors)
    )
