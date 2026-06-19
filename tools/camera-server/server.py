#!/usr/bin/env python3
"""HTTP server: snapshots, MJPEG stream, lens + illuminator control.

Endpoints:
    GET  /            -> image/jpeg (latest frame from the warm pipeline)
    GET  /?full=1     -> image/jpeg at sensor-native resolution (slow ~1-2s)
    GET  /stream      -> multipart/x-mixed-replace MJPEG stream
    GET  /health      -> JSON: backend, AF, lens state, illuminator state
    POST /lens        -> JSON body {"mode": "auto"|"manual", "position": float}
    POST /illuminator -> JSON body {"brightness": int 0..255}
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

# Allow running as a script (python3 server.py) without installing as a package.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from backends import Backend, BackendUnavailable, autodetect, load_backend
    from backends.base import FrameConfig
    from illuminators import Illuminator, NullIlluminator, build_illuminator
else:
    from .backends import Backend, BackendUnavailable, autodetect, load_backend
    from .backends.base import FrameConfig
    from .illuminators import Illuminator, NullIlluminator, build_illuminator


MJPEG_BOUNDARY = "hilcamframe"


class SnapshotHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        path = parts.path.rstrip("/")
        query = parse_qs(parts.query)
        if path in ("", "/"):
            if query.get("full", ["0"])[0] in ("1", "true", "yes"):
                self._serve_snapshot_full()
            else:
                self._serve_snapshot()
        elif path == "/health":
            self._serve_health()
        elif path == "/stream":
            self._serve_stream()
        else:
            self.send_error(404, "not found")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path.rstrip("/")
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            self.send_error(400, f"invalid JSON: {exc}")
            return
        if path == "/lens":
            self._handle_lens(body)
        elif path == "/illuminator":
            self._handle_illuminator(body)
        else:
            self.send_error(404, "not found")

    # ---- GET handlers ----

    def _serve_snapshot(self) -> None:
        try:
            jpeg = self.server.backend.read_jpeg()  # type: ignore[attr-defined]
        except TimeoutError as exc:
            self.send_error(503, str(exc))
            return
        self._send_jpeg(jpeg)

    def _serve_snapshot_full(self) -> None:
        backend: Backend = self.server.backend  # type: ignore[attr-defined]
        try:
            jpeg = backend.capture_full_jpeg()
        except NotImplementedError:
            # Backend can't do native-res; fall back to the warm pipeline frame.
            jpeg = backend.read_jpeg()
        except Exception as exc:  # noqa: BLE001
            self.send_error(503, f"full-res capture failed: {exc}")
            return
        self._send_jpeg(jpeg)

    def _send_jpeg(self, jpeg: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(jpeg)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(jpeg)

    def _serve_health(self) -> None:
        backend: Backend = self.server.backend  # type: ignore[attr-defined]
        illuminator: Illuminator = self.server.illuminator  # type: ignore[attr-defined]
        body = json.dumps(
            {
                "ok": True,
                "backend": backend.name,
                "autofocus": backend.supports_autofocus(),
                "width": backend.cfg.width,
                "height": backend.cfg.height,
                "fps": backend.cfg.fps,
                "lens": backend.get_lens(),
                "illuminator": {
                    "kind": illuminator.kind,
                    "available": illuminator.is_available(),
                    "brightness": illuminator.get_brightness(),
                },
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_stream(self) -> None:
        backend: Backend = self.server.backend  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header(
            "Content-Type",
            f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}",
        )
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last_ts = 0.0
        try:
            while True:
                # Re-read until we get a frame newer than the one we last sent
                # so /stream doesn't burn CPU repeating identical bytes.
                for _ in range(50):
                    with backend._lock:  # noqa: SLF001 — internal coupling is intentional
                        ts = backend._latest_ts  # noqa: SLF001
                        jpeg = backend._latest  # noqa: SLF001
                    if jpeg is not None and ts != last_ts:
                        break
                    time.sleep(0.02)
                else:
                    continue
                last_ts = ts
                part = (
                    f"--{MJPEG_BOUNDARY}\r\n"
                    f"Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(jpeg)}\r\n\r\n"
                ).encode()
                self.wfile.write(part)
                self.wfile.write(jpeg)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            return

    # ---- POST handlers ----

    def _handle_lens(self, body: dict) -> None:
        backend: Backend = self.server.backend  # type: ignore[attr-defined]
        mode = body.get("mode", "auto")
        position = body.get("position")
        try:
            backend.set_lens(mode=mode, position=position)
        except NotImplementedError as exc:
            self.send_error(501, str(exc))
            return
        except ValueError as exc:
            self.send_error(400, str(exc))
            return
        self._send_json({"ok": True, "lens": backend.get_lens()})

    def _handle_illuminator(self, body: dict) -> None:
        illuminator: Illuminator = self.server.illuminator  # type: ignore[attr-defined]
        try:
            brightness = int(body.get("brightness", 0))
        except (TypeError, ValueError) as exc:
            self.send_error(400, f"brightness must be int: {exc}")
            return
        illuminator.set_brightness(brightness)
        self._send_json(
            {
                "ok": True,
                "illuminator": {
                    "kind": illuminator.kind,
                    "available": illuminator.is_available(),
                    "brightness": illuminator.get_brightness(),
                },
            }
        )

    def _send_json(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        if self.path == "/health":
            return
        print(f"{self.address_string()} - {fmt % args}", flush=True)


def build_backend(args) -> Backend:
    cfg = FrameConfig(
        width=args.width,
        height=args.height,
        fps=args.fps,
        jpeg_quality=args.jpeg_quality,
    )
    kwargs: dict = {}
    if args.device:
        kwargs["device"] = args.device
    if args.camera_num is not None:
        kwargs["camera_num"] = args.camera_num

    if args.backend == "auto":
        return autodetect(cfg, **{k: v for k, v in kwargs.items() if k != "device"})
    if args.backend == "picamera2":
        return load_backend("picamera2", cfg, camera_num=args.camera_num or 0)
    if args.backend == "v4l2":
        return load_backend("v4l2", cfg, device=args.device or "/dev/video0")
    raise BackendUnavailable(f"unknown backend: {args.backend}")


def build_illuminator_from_args(args) -> Illuminator:
    if args.no_neopixel:
        return NullIlluminator()
    return build_illuminator(
        kind="neopixel", pin=args.neopixel_pin, count=args.neopixel_count
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="HIL camera snapshot server")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument(
        "--backend",
        choices=("auto", "picamera2", "v4l2"),
        default="auto",
    )
    ap.add_argument("--device", default=None, help="V4L2 device path (v4l2 backend)")
    ap.add_argument(
        "--camera-num", type=int, default=None, help="libcamera camera index (picamera2)"
    )
    # 0 means "use sensor native resolution" — backend-resolved at open
    # time. Raw stream stays pinned to full sensor so FoV is preserved
    # even when main is downscaled.
    ap.add_argument("--width", type=int, default=0)
    ap.add_argument("--height", type=int, default=0)
    ap.add_argument("--fps", type=int, default=5)
    ap.add_argument("--jpeg-quality", type=int, default=85)
    # Illuminator: NeoPixel attached on Adafruit STEMMA 3-pin (default D5,
    # 32 pixels). Override per-host or set --no-neopixel to disable.
    ap.add_argument(
        "--neopixel-pin",
        default="D18",
        help=(
            "Blinka board pin for NeoPixel data line (default D18). "
            "rpi_ws281x only supports D12, D13, D18, D19, D21, D10 — "
            "bridge the ring there if your STEMMA connector lands elsewhere."
        ),
    )
    ap.add_argument("--neopixel-count", type=int, default=32)
    ap.add_argument(
        "--no-neopixel",
        action="store_true",
        help="disable illuminator entirely (force NullIlluminator)",
    )
    args = ap.parse_args()

    try:
        backend = build_backend(args)
    except BackendUnavailable as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)

    illuminator = build_illuminator_from_args(args)
    backend.start()

    server = ThreadingHTTPServer((args.bind, args.port), SnapshotHandler)
    server.backend = backend  # type: ignore[attr-defined]
    server.illuminator = illuminator  # type: ignore[attr-defined]
    print(
        f"HIL snapshot server: backend={backend.name} "
        f"af={backend.supports_autofocus()} "
        f"resolution={backend.cfg.width}x{backend.cfg.height}@{backend.cfg.fps} "
        f"illuminator={illuminator.kind}({'on' if illuminator.is_available() else 'off'}) "
        f"listening on {args.bind}:{args.port}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        backend.stop()
        illuminator.close()


if __name__ == "__main__":
    main()
