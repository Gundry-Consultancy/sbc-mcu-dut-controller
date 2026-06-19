---
name: project-camera-integration
description: Camera monitoring PoC from tyeth/protomq PR #1 being integrated into the HIL controller; plan at docs/CAMERA_INTEGRATION.md
metadata:
  type: project
---

Camera monitoring integration from `tyeth/protomq` PR #1 (branch `video-qr-pytest-capture`, head SHA `5964cf1f6ea7fa7b3ce7246cb9f10a232bbe5d57`).

**Goal:** Port camera monitoring into the HIL controller so admins can configure camera frames and ROI per DUT, with QR auto-detection as the initial hint and manual amendment as the durable setting.

**Current state (as of 2026-05-25, commit bf04757):** Phase 1+2+3 complete. Camera library ported, DB schema updated (new cameras table + camera_rois), UI migrated, REST API implemented. IP camera vertical slice is live and tested (181 tests pass).

**Key decisions made:**
- Library-first — port PR tools as standalone module first (`src/hil_controller/adapters/camera/`)
- New cameras table (not auxes kind=camera) — user chose to migrate away from auxes for cameras
- Hardcoded 13-board calibration data moves to topology.yaml + `camera_rois` DB table
- Functions only from calibration_data.py (compute_scale, transform_roi) are ported; generic (no hardcoded QR_CENTRES)
- solenoid_hub_control.py, hil_exceptions.py, runner_config.py, conftest.py are NOT ported
- IP camera path first (snapshot via HTTP); V4L2 over SSH is a Phase 2 stub

**What's implemented in `src/hil_controller/adapters/camera/`:**
- recorder.py (VideoRecorder), qr_locator.py, frame_extractor.py, calibration.py (math only), report.py
- monitor.py (generic CameraMonitor + ROI dataclass), sources.py (IPCamera + V4L2Camera stub), capture.py

**API endpoints (all under /v1/cameras or /v1/devices/{id}/camera):**
- GET /v1/cameras — list; GET /v1/cameras/{id} — detail; GET /v1/cameras/{id}/snapshot — JPEG frame
- GET /v1/devices/{id}/camera — assignment + ROI; GET /v1/devices/{id}/camera/snapshot — cropped frame
- PUT /v1/devices/{id}/camera/roi — set manual ROI; DELETE — clear ROI
- POST /v1/devices/{id}/camera/calibrate — propose QR ROI; POST .../save — apply

**Remaining work (Phase 4+):**
- V4L2Camera SSH wiring (HostTransport needed)
- VideoRecorder integrated into CameraCapture.stop() for job-duration recording
- Artifact storage (per-job video + distinct frames) in artifact dir
- Prometheus metrics for camera errors/frames
- HTMX cameras list auto-refresh

**Why:** User confirmed camera work belongs in the controller, not in protomq.
**How to apply:** Follow docs/CAMERA_INTEGRATION.md phases; start with Phase 1 (library port, no controller wiring). Check existing camera UI in web/router.py and templates/cameras_*.html before adding new endpoints.
