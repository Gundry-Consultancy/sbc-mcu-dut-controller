---
name: hil-camera-proof
description: "Camera-proof reference for the HIL platform — capture visual evidence (display renders, LED states, physical behaviour) as job assets via the capture_display stage, calibrate/scale per-device ROIs (QR auto-detect or manual, frame-relative warm↔full), pick the right focus driver (pi-camera-server windowed AF in dioptres vs Android ip-webcam full-frame focus_distance), and tune exposure/gain/focus/white-balance for a new panel or board. Also the ops guide for deploying tools/camera-server to a bench host (scripts/deploy-camera-server.sh, host-specific systemd unit, /health verification). Target-app agnostic — WipperSnapper appears only as an example. Use when a job needs a readable photo of a DUT panel, a capture comes out black/blurred/green, an ROI is wrong, or a camera host runs stale code. NOT for: submitting the job itself (hil-job-api), display test authoring (hil-display-pytest / app display skills)."
---

# hil-camera-proof

The bench cameras turn "the firmware says it drew something" into a JPEG a human
can read in a PR. This skill is the full detail behind the
[hil-job-api](../hil-job-api/SKILL.md) "Cameras in one breath" teaser: the
`capture_display` stage and its tuned defaults, the focus-driver capability
tiers, the frame-relative ROI lifecycle, and the camera-server ops appendix.
Job submission, waiting, and asset download are **not** restated here — see
hil-job-api.

## When to use

- A job must produce **visual proof** as an asset: a display render, LED/NeoPixel
  state, eInk refresh, any physical behaviour a log line can't prove.
- **Calibrating** a device's ROI (new DUT on the bench, camera drifted, bench
  rearranged) or grabbing an ad-hoc crop to eyeball a panel.
- **Tuning** capture for a new panel/board — exposure, gain, focus, white
  balance — when the default comes out black, blurred, or green.
- **Deploying/verifying** the camera server on a bench host (appendix).

## The `capture_display` stage

`{"type": "capture_display", …}` in `params.stages`
(`src/hil_controller/adapters/bench_stages.py: _stage_capture_display`).
It autofocuses then **locks** the converged lens position, grabs a
sensor-native still at a **manual** exposure, crops to the ROI, white-balances
the crop, and writes a JPEG to `out`. Pair with `params.collect_artifacts`
(e.g. `["/tmp/*.jpg"]`) so the worker harvests it as a job asset — an image
glob there is also what tells the shared-camera orchestrator this job owns the
focus (see precedence below).

| param | default | meaning |
|---|---|---|
| `camera_url` | *(required)* | camera-server base URL, e.g. `http://rpi-hil006:8080/` |
| `exposure_us` | `32000` | manual exposure for the still (`AeEnable=False`) |
| `gain` | `3.0` | analogue gain for the still |
| `autofocus` | `true` | trigger continuous AF (`POST /lens {"mode":"auto"}`) before the grab |
| `af_settle_s` | `3.0` | how long to let AF converge |
| `focus_lock` | `true` | re-set the converged dioptre as **manual** so the lens can't drift mid-grab |
| `focus_position` | — | override the locked dioptre with an explicit value |
| `white_balance` | `true` | white-patch balance the crop (98th-percentile brightest pixels → neutral) |
| `roi` | — | `[x, y, w, h, frame_w, frame_h]` — omit to keep the full frame |
| `out` | `/tmp/hil-display-capture.jpg` | output path (match your `collect_artifacts` glob) |
| `settle_s` | `0.0` | extra hold after the capture |

Emits `DISPLAY_CAPTURE_VERDICT saved=… exposure_us=… gain=… focus=… wb=… frame=… cropped=…`
— the grep-able contract line. Focus/lens failures are logged and **continue**
(a proof should not fail the job over a lens hiccup); a missing `camera_url` or
an undecodable frame is a hard `StageError`.

### Why the defaults look like that (the tuning story)

Tuned on real hardware against a LilyGo T-Display-S3 i8080 ST7789
(`docs/notes/lilygo-tdisplay-camera-tuning.md`):

- **Exposure 32000 µs @ gain 3.0** — a bright *self-lit* panel on an otherwise
  dark bench fools auto-exposure into crushing it to near-black (the panel is a
  small bright region in a mostly-dark frame). The earlier 6000 µs @ gain 1.0
  proof was unreadable. Sensor-native `?full=1` stills accept
  `&exposure=<us>&gain=<x>` to pin manual controls for that one shot.
- **AF converge, then lock** — continuous AF *drifts during the still grab* and
  blurs the text. The stage triggers auto AF, waits `af_settle_s`, reads the
  converged dioptre back from `/lens`, and re-sets it as manual.
- **White-patch WB in the consumer** — the camera-server applies **no** AWB
  override and *ignores* `awb`/`colour_gains` query params, so the imx708
  green/cyan cast must be neutralised post-capture. The stage rescales channels
  so the brightest pixels (the lit white text) read neutral.
- **Tight ROI** — a loose ROI leaves the panel a dim patch in a black crop.
  Cropped output is upscaled 2× (cubic) for readability.

### Copy-paste stage

```jsonc
{ "type": "capture_display",
  "camera_url": "http://rpi-hil006:8080/",
  "exposure_us": 32000, "gain": 3.0,          // 6000/1.0 is near-black on a dark bench
  "roi": [1270, 770, 235, 135, 2304, 1296],   // x,y,w,h + the frame they were drawn on
  "out": "/tmp/hil-display-capture.jpg" }      // + "collect_artifacts": ["/tmp/*.jpg"] in params
```

Place it *after* whatever makes the panel show something (e.g. an
`inject_protobuf` display Write with a `settle_s` paint window — the WS-Arduino
example; any app the bench can observe works the same way).

## Camera kinds & focus drivers

The controller resolves a per-camera **focus driver** from `cameras.kind`
(fallback: source-URL heuristic — `…/shot.jpg|photo.jpg` → ip-webcam, other
http(s) → pi-camera-server). Drivers translate one camera-agnostic directive
into native calls (`adapters/camera/focus_drivers.py`). Manual-focus values are
in the driver's **native units** — they do not translate between kinds.

| | `pi-camera-server` | `ip-webcam` (Android app) |
|---|---|---|
| hardware | Pi CSI via picamera2/libcamera, or UVC via v4l2 (`tools/camera-server`) | phone running IP Webcam |
| windowed AF | **yes** — ROI maps to `AfWindows` (`POST /lens {"mode":"window",…}`) | **no** — full-frame AF only; ROI window is dropped, degrades to continuous-picture AF + `/focus` trigger |
| manual focus | dioptres, `0..` (sensor-specific max) via `POST /lens {"mode":"manual","position":…}` | `focus_distance` `0.0–10.0` (0.1 steps) via `/settings/focusmode?set=off` + `/settings/focus_distance?set=…` |
| manual exposure | `?full=1&exposure=<µs>&gain=<x>` per-still | `manual_sensor=on` + `iso` + `exposure_ns` via `/settings/{key}?set={value}` (ranges vary per phone — see the ip-webcam-api-reference skill) |
| illuminator | `POST /illuminator {"brightness":0–255}` (NeoPixel ring; Null fallback if absent) | torch on/off (`/enabletorch`, `/disabletorch`) |
| unknown kind | — | — (no-op driver: logged, skipped) |

**All focus/illuminator pushes are best-effort** — failures are logged and
swallowed. A job never fails because a camera is unreachable.

**Focus precedence** on a shared camera (`adapters/camera/orchestrator.py`):
1. explicit device request (`POST /v1/devices/{id}/camera/focus`, the "focus
   this DUT now" path);
2. else the most-recently-created **active job** on the camera whose
   `collect_artifacts` includes an image glob → windowed AF on that device's ROI;
3. else the **mean** of active devices' `manual_focus` values;
4. else plain continuous auto. Illuminator brightness is the max across active
   devices.

## ROI lifecycle

ROIs are **frame-relative**: stored as `(x, y, w, h)` **plus** the frame size
they were drawn on (`roi_frame_width/height`). Every consumer scales by
`actual_dims / roi_frame_dims`, so one ROI is valid against the fast **warm**
frame (e.g. 2304×1296 on imx708) *and* the sensor-native **full** still
(4608×2592 — 2×). A bare warm-frame ROI applied raw to a full still lands on
empty bench — always carry the frame size.

Endpoints (full detail: `docs/api.md` "Cameras & ROIs"):

- **Calibrate (QR auto-detect):** `POST /v1/devices/{id}/camera/calibrate`
  proposes an ROI from the device's QR sticker (+ confidence + method);
  `…/calibrate/save` persists it, recording the detection frame automatically.
- **Manual:** `PUT /v1/devices/{id}/camera/roi` with
  `{x, y, w, h, frame_width?, frame_height?}` — omit the frame size and the
  controller detects it from a live snapshot. `DELETE` clears. The web UI has a
  drag-to-draw editor on the same endpoint.
- **Ad-hoc crop:** `GET /v1/devices/{id}/camera/snapshot?res=full&pad=0.05` —
  a sharp ROI crop of the DUT panel, no job needed. `res=warm` (default) is the
  fast path; `res=full` reconfigures to still mode (~1–2 s) and is **heavy on
  weak hosts** (a Pi Zero 2 W wedges if you poll it) — capture one-shot. Note
  this path runs auto-exposure; for self-lit panels use `capture_display`.

## Appendix: deploying the camera server

Canonical source: `tools/camera-server/` (server.py + picamera2/v4l2 backends +
NeoPixel illuminator with graceful Null fallback). The deployment on each CSI
host **tracks the repo** — never hand-edit `/home/pi/hil-camera-server`:

```bash
scripts/deploy-camera-server.sh rpi-hil006     # [pi@]<host>
```

It backs up the existing deployment, untars the **runtime code only**
(`server.py`, `backends/`, `illuminators/`, `tuning/`), restarts
`hil-camera.service`, and prints `/health`. It deliberately does **not** sync
`hil-camera.service`: the systemd unit is **host-specific** (e.g. rpi-hil006
has no NeoPixel ring, so its `ExecStart` carries `--no-neopixel`) — manage the
unit per host.

**GOTCHA (2026-07-02):** a host whose unit points somewhere *else* silently
keeps running stale code after a deploy. rpi-displays' unit pointed at an old
`/home/pi/usbip-hil-controller` checkout — the deploy synced
`/home/pi/hil-camera-server`, the service restarted fine, and served the old
build. After any deploy, check the unit actually runs the deploy target:

```bash
ssh pi@<host> systemctl cat hil-camera.service   # ExecStart must run /home/pi/hil-camera-server
```

Then verify the build via `GET /health` (backend, AF, lens + illuminator
state): on current code the lens object includes `window` / `af_window_px`
fields — their **absence means a pre-ROI-autofocus build** is still running.
First-time install (apt deps, IMX519 AF tuning patch) is in
`tools/camera-server/README.md`.
