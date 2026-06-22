"""Controller-side webcam capture for remote-display HIL tests.

For an eInk/TFT wired to a remote SBC, the pytest runs on the SBC and drives the
panel in-process; the *capture* happens here on the controller. The SBC test
prints ``WS_HIL_CAPTURE seq=N label=L kind=snap|splash window_s=W`` markers to
stdout as it reaches each stage; the worker streams those lines into
:meth:`HilCapture.feed`, and a concurrent :meth:`HilCapture.consume` task turns
each marker into a proof frame (full frame + a tight ROI crop) under
``snapshot_dir`` for harvesting.

Self-contained from a ``params.capture`` block (webcam_url + optional ROI +
snapshot_dir + tune profile) so it needs no DB/device coupling — the submitter
already knows the camera framing (the same WS_DISPLAY_ROI the ILI9341 CI passes).

``kind="snap"`` is a one-shot grab of the held panel; ``kind="splash"`` samples a
``window_s`` window (the slow eInk add is still running) and keeps the first
*settled* frame that differs from the pre-add baseline — the boot splash, before
the status bar paints over it. Degrades to full frames when cv2 is unavailable.
"""

from __future__ import annotations

import asyncio
import logging
import re
import urllib.parse
from pathlib import Path
from typing import Any

from hil_controller.adapters.camera import roi_snapshot

log = logging.getLogger(__name__)

_MARKER = re.compile(
    r"WS_HIL_CAPTURE\s+seq=(?P<seq>\d+)\s+label=(?P<label>\S+)\s+"
    r"kind=(?P<kind>\S+)(?:\s+window_s=(?P<window>[\d.]+))?"
)


async def _fetch(url: str, timeout: float = 10.0) -> bytes | None:
    try:
        import httpx

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content
    except Exception as exc:  # pragma: no cover - network best-effort
        log.debug("hil_capture fetch %s failed: %s", url, exc)
        return None


class HilCapture:
    """Coordinates per-stage webcam proof for one remote-display HIL run."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.webcam_url: str = cfg.get("webcam_url") or ""
        self.full_url: str = (
            roi_snapshot.full_res_url(self.webcam_url, cfg.get("streams")) or self.webcam_url
        )
        roi = cfg.get("roi")
        self.roi = tuple(int(v) for v in roi) if roi and len(roi) >= 4 else None
        self.ref_w = cfg.get("roi_frame_width")
        self.ref_h = cfg.get("roi_frame_height")
        self.snapshot_dir = Path(cfg.get("snapshot_dir") or "/tmp/hil-proof")
        self.tune_cfg = cfg.get("tune") or {}
        self.snapshots: list[Path] = []
        self._q: asyncio.Queue = asyncio.Queue()
        self._baseline_gray: Any = None  # cv2 grayscale ROI of the pre-add frame

    # -- producer side (sync, called from the transport's on_line) ---------- #

    def feed(self, line: str) -> None:
        m = _MARKER.search(line)
        if not m:
            return
        self._q.put_nowait(
            {
                "seq": int(m["seq"]),
                "label": m["label"],
                "kind": m["kind"],
                "window_s": float(m["window"]) if m["window"] else 0.0,
            }
        )

    def close(self) -> None:
        self._q.put_nowait(None)

    # -- consumer side (async, run concurrently with the SBC test run) ------- #

    async def consume(self) -> None:
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        for old in self.snapshot_dir.glob("*.jpg"):  # only this run's frames
            try:
                old.unlink()
            except OSError:
                pass
        await self._tune()
        while True:
            item = await self._q.get()
            if item is None:
                return
            try:
                if item["kind"] == "splash":
                    await self._splash(item["seq"], item["label"], item["window_s"])
                else:
                    await self._snap(item["seq"], item["label"])
            except Exception:  # pragma: no cover - capture must not kill the run
                log.exception("hil_capture stage %s failed", item.get("label"))

    async def _tune(self) -> None:
        if not self.tune_cfg or not self.webcam_url:
            return
        base = self.webcam_url.rsplit("/", 1)[0]
        profile = {"manual_sensor": "on", **{k: str(v) for k, v in self.tune_cfg.items()}}
        for key, val in profile.items():
            await _fetch(f"{base}/settings/{key}?set={urllib.parse.quote(val)}", timeout=5)
        log.info("hil_capture tuned webcam: %s", profile)

    def _save(self, frame: bytes, seq: int, label: str) -> None:
        full_dest = self.snapshot_dir / f"{seq:02d}_{label}.jpg"
        full_dest.write_bytes(frame)
        self.snapshots.append(full_dest)
        if self.roi:
            x, y, w, h = self.roi[:4]
            crop = roi_snapshot.crop_to_jpeg(
                frame, x=x, y=y, w=w, h=h, ref_w=self.ref_w, ref_h=self.ref_h, pad=0.05
            )
            if crop:
                roi_dest = self.snapshot_dir / f"{seq:02d}_{label}_roi.jpg"
                roi_dest.write_bytes(crop)
                self.snapshots.append(roi_dest)
        log.info("hil_capture %02d %s -> %s", seq, label, full_dest)

    async def _snap(self, seq: int, label: str) -> None:
        frame = await _fetch(self.full_url)
        if frame is None:
            log.warning("hil_capture %02d %s: no frame", seq, label)
            return
        if label == "baseline_pre_add":
            self._baseline_gray = self._roi_gray(frame)
        self._save(frame, seq, label)

    # -- splash selection (reflective/lit panel, mode="epd"-style) ---------- #

    def _roi_gray(self, frame: bytes) -> Any:
        try:
            import cv2
            import numpy as np
        except ImportError:  # pragma: no cover
            return None
        img = cv2.imdecode(np.frombuffer(frame, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        if self.roi:
            th, tw = img.shape[:2]
            x, y, w, h = roi_snapshot.scale_box(
                *self.roi[:4], self.ref_w, self.ref_h, tw, th
            )
            img = img[y : y + h, x : x + w]
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    async def _splash(self, seq: int, label: str, window_s: float) -> None:
        try:
            import cv2  # noqa: F401
            import numpy as np  # noqa: F401
        except ImportError:  # pragma: no cover - no cv2: just grab one frame
            await self._snap(seq, label)
            return
        samples: list[tuple[bytes, Any]] = []  # (frame_bytes, roi_gray)
        deadline = asyncio.get_event_loop().time() + max(window_s, 1.0)
        while asyncio.get_event_loop().time() < deadline:
            frame = await _fetch(self.full_url)
            if frame is not None:
                g = self._roi_gray(frame)
                if g is not None:
                    samples.append((frame, g))
            await asyncio.sleep(0.4)

        chosen = self._pick_settled(samples)
        if chosen is None:
            log.warning("hil_capture %02d %s: no settled frame in %d samples",
                        seq, label, len(samples))
            if samples:
                self._save(samples[-1][0], seq, label)
            return
        self._save(chosen, seq, label)

    def _pick_settled(self, samples: list[tuple[bytes, Any]]) -> bytes | None:
        """First frame that differs from the pre-add baseline and is settled
        (small diff to a neighbour) — the eInk boot splash."""
        import numpy as np

        if not samples:
            return None

        def _diff(a: Any, b: Any) -> float:
            if a is None or b is None or a.shape != b.shape:
                return 1e9
            return float(np.mean(np.abs(a.astype("int16") - b.astype("int16"))))

        base = self._baseline_gray
        change_min, settle_max = 18.0, 16.0
        relaxed: bytes | None = None
        relaxed_d = -1.0
        for j, (frame, g) in enumerate(samples):
            d_base = _diff(g, base) if base is not None else 1e9
            if base is not None and d_base < change_min:
                continue  # still the pre-state
            nbrs = [samples[k][1] for k in (j - 1, j + 1) if 0 <= k < len(samples)]
            settle = min((_diff(g, n) for n in nbrs), default=0.0)
            if d_base > relaxed_d:
                relaxed, relaxed_d = frame, d_base
            if settle <= settle_max:
                return frame
        return relaxed
