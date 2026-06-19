"""Camera backend implementations.

Each backend wraps a different camera-access stack (libcamera/picamera2,
V4L2/UVC, …) behind a common interface so the HTTP server stays generic.
Backends are loaded lazily via try-import so each camera host only needs
the libraries for its own hardware.
"""
from .base import Backend, BackendUnavailable, autodetect, load_backend

__all__ = ["Backend", "BackendUnavailable", "autodetect", "load_backend"]
