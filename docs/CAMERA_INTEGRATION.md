# Camera Monitoring Integration Plan

Status: **Draft v0.1** — implementation plan, library-first approach.

## 1. Context

The [`tyeth/protomq` PR #1](https://github.com/tyeth/protomq/pull/1) contains a
proof-of-concept camera monitoring pipeline for display testing. It works but
is tightly coupled to a fixed set of 13 hardcoded boards (Adafruit `adafru.it`
short-URLs), protomq-specific calibration data, and pytest conftest machinery.

This document describes how to port the *reusable mechanics* into the HIL
controller so that admins can configure camera frames and ROI per DUT, with QR
code auto-detection as the initial hint and manual amendment as the durable
setting.

## 2. What we keep from the PR (and what we don't)

### Ported as-is (de-hardcoded)

| PR file | Ported to | Notes |
|---------|-----------|-------|
| `tools/video_capture.py` | `adapters/camera/recorder.py` | `VideoRecorder`, unchanged logic |
| `tools/qr_locator.py` | `adapters/camera/qr_locator.py` | QR scan + GrabCut/Otsu segmentation; unchanged |
| `tools/frame_extractor.py` | `adapters/camera/frame_extractor.py` | Distinct-frame + change-type classification |
| `tools/display_comparator.py` | `adapters/camera/report.py` | HTML report generator |
| `tools/calibration_data.py` (functions only) | `adapters/camera/calibration.py` | `compute_scale()`, `transform_roi()` — the math. **Not** `QR_CENTRES`, `REFERENCE_ROIS`, `YELLOW_BOX_ROIS`, `BOARD_REVISIONS` |

### Not ported — data moves to topology + DB

| PR constant | Where it goes |
|-------------|---------------|
| `REFERENCE_ROIS` | `camera_rois` DB table (per DUT, admin-editable) |
| `QR_CENTRES` | `Device.qr_identifier` in topology.yaml |
| `YELLOW_BOX_ROIS` | Initial data in `camera_rois` for bootstrap |
| `BOARD_REVISIONS` | `runner_config.py`'s `BoardInfo` is a separate concern; not part of camera work |

### Explicitly excluded

- `tools/solenoid_hub_control.py` — already covered by `Mcp23017Solenoid` adapter (§10.2 ARCHITECTURE.md)
- `hil_exceptions.py` — cross-cutting concern, separate work item
- `runner_config.py` — absorbed into the topology manifest + auth policy
- `conftest.py` pytest fixtures — the HIL controller runs scripts, not pytest directly

## 3. Camera topology

Two camera types are in use today; the model must handle both:

```
rpi-displays (192.168.1.234, Pi Zero 2W)
  └── CSI ribbon camera
        → captures microcontroller-fleet DUT displays
        → OpenCV device index on rpi-displays; accessed via SSH from controller

IP Webcam (Android phone, ~192.168.1.X:8080)
  → HTTP MJPEG stream, no SSH needed
  → covers rpi-hil00x SBC-fleet displays and the Tachyon itself
  → OpenCV: cv2.VideoCapture("http://<phone>:8080/video")
```

Future: additional cameras splitting DUTs per camera (e.g. one camera per
quadrant of the bench). The model supports this by making camera assignment
per-device in topology.yaml.

### Source URI scheme

```
v4l2:<device_index_or_path>   # e.g. "v4l2:0" or "v4l2:/dev/video0"
                               # device lives on the camera's host_id
                               # frames fetched via SSH HostTransport

http://<host>:<port>/path      # e.g. "http://192.168.1.X:8080/video"
                               # accessed directly by the controller process
                               # cv2.VideoCapture(url) or requests+PIL
```

## 4. File layout

```
src/hil_controller/
  adapters/
    camera/
      __init__.py              # exports CameraAdapter, CameraMonitor, ROI
      recorder.py              # VideoRecorder — OpenCV writer in background thread
      qr_locator.py            # scan_qr_codes(), segment_board_roi(), locate_all_boards()
      frame_extractor.py       # Frame dataclass, extract_distinct_frames()
      calibration.py           # compute_scale(), transform_roi() (math only)
      monitor.py               # CameraMonitor — generic BoardMonitor replacement
      report.py                # generate_report() HTML
      capture.py               # CameraCapture — the DeviceAdapter-layer entry point
      sources.py               # CameraSource protocol: V4L2Camera, IPCamera
```

## 5. Topology schema additions

```yaml
# /etc/hil/topology.yaml  (additions to existing schema)

cameras:
  - id: csi-rpi-displays
    host_id: rpi-displays          # camera lives on this HIL host (v4l2)
    source: "v4l2:0"
    resolution: [1280, 720]
    fps: 30
    notes: "CSI ribbon camera on Pi Zero 2W, fixed position over bench"

  - id: ip-webcam-android
    host_id: null                  # network camera, no HIL host
    source: "http://192.168.1.X:8080/video"   # filled in during deploy
    resolution: [1280, 720]
    fps: 15
    notes: "Android IP Webcam app, covers rpi-hil00x fleet and Tachyon"

devices:
  - id: qtpy-s3-01
    ...
    camera_id: csi-rpi-displays    # which camera sees this DUT
    qr_identifier: "https://adafru.it/5300"   # QR URL for auto-ROI; null if no QR
```

### New topology fields

| Entity | Field | Type | Notes |
|--------|-------|------|-------|
| Camera | `id` | string | Stable slug |
| Camera | `host_id` | string? | FK to Host if v4l2, null if HTTP stream |
| Camera | `source` | string | `v4l2:<idx>` or `http://...` |
| Camera | `resolution` | [int, int] | [w, h] |
| Camera | `fps` | float | Target FPS for recording |
| Device | `camera_id` | string? | FK to Camera; null if no camera |
| Device | `qr_identifier` | string? | QR data string for auto-ROI; null if none |

## 6. Database additions

Two new tables; seeded from topology.yaml on startup, camera_rois live-editable
by admins without a git commit:

```sql
-- Cameras known to the controller (seeded from topology.yaml)
CREATE TABLE IF NOT EXISTS cameras (
    id          TEXT PRIMARY KEY,
    host_id     TEXT,                      -- NULL for HTTP cameras
    source      TEXT NOT NULL,             -- "v4l2:0" or "http://..."
    resolution_w INTEGER,
    resolution_h INTEGER,
    fps         REAL,
    status      TEXT DEFAULT 'online',     -- 'online' | 'offline'
    notes       TEXT
);

-- Per-device ROI (x, y, w, h in camera pixel space); admin-editable.
-- FRAME-RELATIVE: roi_frame_width/height record the frame the ROI was drawn on.
CREATE TABLE IF NOT EXISTS camera_rois (
    device_id        TEXT PRIMARY KEY,     -- FK to Device.id
    camera_id        TEXT NOT NULL,        -- FK to cameras.id
    x                INTEGER NOT NULL,
    y                INTEGER NOT NULL,
    w                INTEGER NOT NULL,
    h                INTEGER NOT NULL,
    roi_frame_width  INTEGER,              -- frame W the ROI was calibrated against
    roi_frame_height INTEGER,              -- frame H the ROI was calibrated against
    source           TEXT NOT NULL DEFAULT 'manual',  -- 'qr_auto' | 'yellow_box' | 'manual'
    confidence       REAL,                 -- 0.0–1.0, set by QR auto-detect
    updated_at       TEXT NOT NULL         -- ISO 8601 UTC timestamp
);
```

### Frame-relative ROI (why `roi_frame_*` matters)

A camera frames a whole bench, so a DUT's display is only a small patch of the
sensor. The Pi camera-server serves that patch at two sizes — a fast **warm**
frame (e.g. `2328×1748`) and a sensor-native **full** still (`4656×3496`, exactly
2× here). A bare `(x,y,w,h)` is meaningless without knowing which frame it was
measured in: applied to the full still, a warm-frame ROI lands at half scale on
the wrong region. Storing `roi_frame_width/height` makes the ROI **relative** —
any consumer scales by `target_dims / roi_frame_dims`, so the same ROI yields a
correct crop at warm res, full res, or after the camera's FOV/resolution changes.
The scaling helpers live in `adapters/camera/roi_snapshot.py`
(`full_res_url`, `scale_box`, `crop_to_jpeg`), shared by the `/v1` and `/ui`
snapshot routes. Legacy ROIs (no recorded frame) are back-filled from the warm
frame on the first full-res request.

`camera_rois` is deliberately separate from the topology manifest so admins
can amend a ROI live (the camera drifted, bench was rearranged) without
touching git. The manifest's `qr_identifier` is used only as the *initial
seed hint*; the ROI stored in DB is the authoritative crop.

## 7. Camera source abstraction

```python
# adapters/camera/sources.py

from typing import Protocol, AsyncIterator
import numpy as np

class CameraSource(Protocol):
    """Read frames from a camera, abstracting v4l2 vs HTTP."""

    async def read_frame(self) -> np.ndarray: ...
    async def read_frames(self) -> AsyncIterator[np.ndarray]: ...
    async def close(self) -> None: ...


class V4L2Camera:
    """
    Captures frames from a v4l2 device on a remote HIL host via SSH.

    On each read_frame(), runs a small Python one-liner on the host that:
      1. opens cv2.VideoCapture(device_index)
      2. reads one frame
      3. writes a JPEG to stdout
    Then copy_from() pulls it to /tmp on the controller.

    For continuous capture (recording), a remote-side script is kept alive
    over a streaming SSH channel.
    """
    def __init__(self, transport, device_index: int): ...


class IPCamera:
    """
    Reads frames from an HTTP MJPEG stream (Android IP Webcam, etc).

    Uses cv2.VideoCapture(url) directly on the controller process.
    No SSH involved.
    """
    def __init__(self, url: str): ...
```

## 8. CameraMonitor (generic BoardMonitor replacement)

`monitor.py` replaces the PR's `BoardMonitor`, removing the 13-board
hardcoding:

```python
class CameraMonitor:
    """
    Continuously reads frames from a CameraSource, runs QR detection to
    calibrate scale/offset, and maintains per-device ROI crops.

    ROIs come from DB (camera_rois table), not hardcoded.
    QR re-detection runs periodically to refresh scale; results are written
    back to camera_rois with source='qr_auto'.

    Thread-safe; get_crop(device_id) can be called from any thread.
    """

    def __init__(
        self,
        source: CameraSource,
        rois: dict[str, ROI],          # {device_id: ROI} loaded from DB
        qr_map: dict[str, str],        # {qr_data: device_id} from topology
        capture_interval: float = 1.0,
        scale_update_interval: float = 10.0,
        archive: bool = False,
        output_dir: Path | None = None,
    ): ...

    def get_crop(self, device_id: str) -> np.ndarray | None: ...
    def get_roi(self, device_id: str) -> ROI | None: ...
    def available_devices(self) -> list[str]: ...
```

`ROI` is a small dataclass: `x, y, w, h, source, confidence, updated_at`.

## 9. CameraCapture adapter

`capture.py` is the DeviceAdapter-layer entry point, wired in at job runtime:

```python
class CameraCapture:
    """
    Per-job camera capture. Started after acquire(), stopped at release().

    1. Grabs the device's ROI from DB.
    2. If no ROI exists, attempts QR auto-detection in the first N frames
       and persists the result (source='qr_auto').
    3. Records video for the job duration via VideoRecorder.
    4. Extracts distinct frames at job end.
    5. Pulls artifacts to the controller's per-job artifact directory.
    """

    def __init__(
        self,
        device_id: str,
        camera_source: CameraSource,
        roi_store: ROIStore,           # thin DB wrapper
        artifact_dir: Path,
    ): ...

    async def start(self) -> None: ...
    async def stop(self) -> CameraArtifacts: ...
    async def calibrate(self) -> ROI | None:
        """Force a QR scan of the current frame; return updated ROI."""
        ...
```

`ROIStore` is a minimal DB wrapper (`get_roi(device_id)`,
`set_roi(device_id, roi)`); injected so the adapter doesn't import SQLAlchemy
directly.

## 10. Admin API endpoints

All under `/v1` with the existing bearer-token auth:

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/v1/cameras` | List cameras with current status |
| GET | `/v1/cameras/{id}/snapshot` | Return a single JPEG frame (multipart/mixed or redirect) |
| GET | `/v1/devices/{id}/camera` | Camera assignment + current ROI for this device |
| GET | `/v1/devices/{id}/camera/snapshot?res=warm\|full&pad=` | Current frame cropped to device ROI (full-res scales the ROI from `roi_frame_*`) |
| PUT | `/v1/devices/{id}/camera/roi` | Set manual ROI `{x, y, w, h, frame_width?, frame_height?}` |
| DELETE | `/v1/devices/{id}/camera/roi` | Clear manual override (revert to qr_auto) |
| POST | `/v1/devices/{id}/camera/calibrate` | Trigger QR auto-detect on a live frame; returns proposed ROI + frame size (does not save) |
| POST | `/v1/devices/{id}/camera/calibrate/save` | Apply QR auto-detect result and save to DB |

See [api.md](api.md#cameras--rois) for the full request/response detail and the
`res`/`pad` query params on the snapshot endpoint.

### PUT body for manual ROI

```json
{ "x": 230, "y": 415, "w": 320, "h": 280, "frame_width": 2328, "frame_height": 1748 }
```

`frame_width/height` are the size of the frame the ROI was drawn on; omit them and
the controller detects them from a live snapshot. Response includes
`"roi_frame_width"`, `"roi_frame_height"`, `"source": "manual"`, `"updated_at"`.

### POST /calibrate response

```json
{
  "found": true,
  "qr_data": "https://adafru.it/5300",
  "roi": { "x": 230, "y": 415, "w": 320, "h": 280 },
  "confidence": 0.91,
  "method": "grabcut"    // or "otsu" or "padding_fallback"
}
```

When `found: false`, returns the reason (`no_qr_detected`, `qr_mismatch`,
`segmentation_failed`) and the full-frame snapshot for visual inspection.

## 11. Admin dashboard UI (HTMX)

New panel on the device detail page (`/devices/{id}`):

```
┌─────────────────────────────────────────────┐
│ Camera: csi-rpi-displays                    │
│                                             │
│ [Live snapshot thumbnail]                   │
│                                             │
│ ROI:  x=230 y=415 w=320 h=280  (qr_auto)  │
│ Updated: 2026-04-08T14:47Z                  │
│                                             │
│ [Re-detect QR]  [Edit ROI manually]         │
│ [View cropped snapshot]                     │
└─────────────────────────────────────────────┘
```

- **Re-detect QR** → `POST /v1/devices/{id}/camera/calibrate`, shows
  proposed ROI overlaid on snapshot; admin clicks **Save** or **Discard**.
- **Edit ROI manually** → inline x/y/w/h fields + a pixel-drag selector
  on the snapshot image (optional, nice-to-have).
- **View cropped snapshot** → `GET /v1/devices/{id}/camera/snapshot`,
  shows the ROI crop.

The thumbnail auto-refreshes every 5 s via HTMX `hx-trigger="every 5s"`.

## 12. Phased delivery

This is M5 work in the architecture milestones, but the library-first approach
lets us land pieces independently:

### Phase 1 — Camera library (no controller wiring)
Port PR tools, remove all hardcoding. Standalone module that can be imported
and tested without FastAPI, SQLite, or SSH. Includes:
- `recorder.py`, `qr_locator.py`, `frame_extractor.py`, `calibration.py`,
  `report.py`
- `sources.py` with `IPCamera` implementation (network cameras are the
  simpler path, no SSH needed)
- `monitor.py` — generic `CameraMonitor` with injected ROI + QR maps
- Unit tests (fake camera source returning synthetic frames)

### Phase 2 — Topology + DB schema
- Add `cameras` and `camera_rois` tables
- Add `camera_id` and `qr_identifier` to Device model
- Topology YAML loader reads camera blocks
- `ROIStore` DB wrapper
- `V4L2Camera` implementation over `HostTransport` (needs M3 SSH transport)

### Phase 3 — CameraCapture adapter
- `capture.py` wired to the job worker
- Artifact storage (video + distinct frames) in per-job artifact dir
- QR auto-calibration at job start if ROI not yet set

### Phase 4 — Admin API + HTMX dashboard
- REST endpoints (§10)
- HTMX device detail panel (§11)
- Snapshot serving

## 13. Dependencies

New Python packages (camera library only):

```toml
# pyproject.toml additions
opencv-python = ">=4.8"      # cv2 — VideoCapture, VideoWriter, GrabCut
pyzbar = ">=0.1.9"           # QR decoding
numpy = ">=1.24"             # image array operations
```

`pyzbar` needs `libzbar0` on the host OS (`apt install libzbar0`).

For the IP Webcam path, `opencv-python` alone suffices. For v4l2-over-SSH,
the HIL host also needs `python3-opencv` (or a venv with `opencv-python`).

## 14. Open questions

1. **IP Webcam URL** — the phone's IP is not in the repo. Should it be in
   `/etc/hil/topology.yaml` as a plain field, or treated as a secret? It's
   not a credential, just a LAN address — topology.yaml is fine unless the
   camera stream is on a routable network.

2. **Frame pulling strategy for V4L2** — two options:
   - (a) Run a short-lived `python3 -c "..."` on the HIL host per frame
     (simple, no persistent process).
   - (b) Keep a persistent remote script alive over an SSH channel
     streaming raw JPEG frames back (lower latency, more complex).
   Recommend (a) for Phase 2 (grab-frame-on-demand) and (b) for Phase 3
   (continuous recording during jobs).

3. **Tachyon in camera view** — the IP Webcam covers the Tachyon itself.
   Should the Tachyon appear as a "device" with a ROI, or is this just
   ambient coverage not tied to a job? Suggest: add the Tachyon as a
   special `kind: controller` device in topology so the dashboard can show
   its crop; it doesn't participate in job scheduling.

4. **Snapshot auth** — should snapshot endpoints be read-only public (like
   the dashboard) or require a bearer token? Frames may show test firmware
   output including credentials in logs displayed on-screen. Recommend:
   require token (same as other `/v1/` endpoints), but add a dashboard
   iframe with token-bearing HTMX so the admin panel works without
   re-authing.

5. **QR calibration reference image** — the PR's `calibration_data.py`
   measured QR centres from a specific 2560×2092px reference photo. For
   the controller, the reference frame is whatever the camera currently
   sees; `compute_scale()` / `transform_roi()` work off live QR detections,
   not a stored reference image. No reference image needs to be checked in.

6. **Bootstrap ROIs** — for the 13 known boards already on the bench, the
   PR's `YELLOW_BOX_ROIS` values can seed `camera_rois` during initial
   migration. A one-off `scripts/seed_camera_rois.py` can insert these as
   `source='yellow_box'` so admins see something immediately and can
   refine per DUT.

## 15. Follow-ups (designed, not yet built)

These were scoped alongside the frame-relative ROI work and deliberately deferred.

### 15.1 Manual sensor settings for bright TFT / eInk screens

Bright self-lit displays (TFT) and high-contrast eInk fool auto-exposure/auto-ISO,
washing out or crushing the very region we crop. The fix is per-camera/per-device
**capture profiles** (e.g. a `bright_screen` preset) applied before a full-res
capture, then reverted.

- **Pi camera-server** (`tools/camera-server`): add a `POST /controls` endpoint
  wrapping picamera2 `set_controls` — `ExposureTime` (µs), `AnalogueGain` (≈ISO/100),
  `FrameDurationLimits` (min/max µs), `AeEnable=False` for manual. The backend
  already does `set_controls` for AF/lens, so this is additive; apply the controls
  around the `capture_full_jpeg` still reconfigure and restore afterwards.
- **Android IP Webcam** cameras: passthrough via `GET /settings/{key}?set={value}`.
  Observed manual setup (from a real bright-TFT `status.json`):
  `manual_sensor=on`, `iso=524`, `exposure_ns=7170511`, `frame_duration=16665880`
  (≈60 fps cap). Valid per-device value ranges differ by phone — use the
  `anthropic-skills:ip-webcam-api-reference` skill to validate before sending.
- **Storage**: a `capture_settings_json` column (per-camera default + optional
  per-device override), or a small `capture_profiles` table keyed by name. The
  controller sends the profile to the camera before `res=full` capture.

### 15.2 Auto-focus window from the ROI

Point the camera's AF metering at the DUT instead of the whole bench: picamera2
`AfMetering=Windows` + `AfWindows=[(x,y,w,h)]` in sensor coords. The ROI (now
frame-relative) already provides the box — scale it to the AF coordinate space and
push it via the camera-server lens/controls endpoint. Nice-to-have; not required
for correct crops.
