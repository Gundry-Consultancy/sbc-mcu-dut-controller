# HIL Camera Snapshot Server

A small HTTP server that keeps a camera pipeline warm and serves fresh JPEG snapshots or an MJPEG stream. Pluggable backends keep it portable across libcamera Pi CSI sensors, UVC webcams, and future stacks.

## Endpoints

| Path                | Response                                                                 |
|---------------------|--------------------------------------------------------------------------|
| `GET /`             | `image/jpeg` — latest frame from the warm pipeline (sub-100ms when warm) |
| `GET /?full=1`      | `image/jpeg` at sensor-native resolution (reconfigures pipeline; ~1-2s)  |
| `GET /stream`       | `multipart/x-mixed-replace` MJPEG stream (record + split frames client-side) |
| `GET /health`       | JSON: backend, AF, lens state, illuminator state                         |
| `POST /lens`        | `{"mode": "auto"\|"manual", "position": float}` — override continuous AF |
| `POST /illuminator` | `{"brightness": 0..255}` — drive the NeoPixel ring                       |

## Backends

| Name        | Hardware                          | AF                                | Deps                        |
|-------------|-----------------------------------|-----------------------------------|-----------------------------|
| `picamera2` | Pi CSI sensors via libcamera      | Continuous AF (full range)        | `python3-picamera2`         |
| `v4l2`      | UVC webcams via OpenCV/V4L2       | UVC `focus_auto` if exposed       | `python3-opencv`            |

`--backend auto` (default) tries each in order and uses the first that opens. Force a specific backend with `--backend picamera2` or `--backend v4l2`.

## Install on a camera host

Pi CSI host (e.g. rpi-displays):

```bash
sudo apt install -y python3-picamera2
git clone https://github.com/Gundry-Consultancy/usbip-hil-controller.git ~/usbip-hil-controller

# IMX519 only: graft the AF block into libcamera's tuning file. Upstream
# omits it even though the IPA library has the AF algorithm compiled in.
sudo python3 ~/usbip-hil-controller/tools/camera-server/tuning/imx519-af-patch.py

sudo cp ~/usbip-hil-controller/tools/camera-server/hil-camera.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hil-camera
```

UVC webcam host:

```bash
sudo apt install -y python3-opencv v4l-utils
# then the same systemd steps; --backend auto picks v4l2 when picamera2 isn't installed
```

## CLI

```
server.py [--port 8080] [--backend auto|picamera2|v4l2]
          [--device /dev/video0] [--camera-num 0]
          [--width 0] [--height 0] [--fps 5] [--jpeg-quality 85]
          [--neopixel-pin D5] [--neopixel-count 32] [--no-neopixel]
```

## Illuminator (NeoPixel ring)

The server can drive a NeoPixel ring attached to the host — useful for lighting the DUTs uniformly when the camera is in a dark enclosure. Wiring assumption is an Adafruit STEMMA 3-pin connector (GND / +V / signal) on the eInk Bonnet or similar; the signal line lands on a Pi GPIO that's configurable per host.

- `--neopixel-pin D18` — Blinka board pin name. `rpi_ws281x` only supports a fixed set: **D12, D13, D18, D19, D21** (PWM/PCM) and **D10** (SPI). Other pins (including D5/D6 on common Adafruit bonnets) fail at `ws2811_init` with "Selected GPIO not possible" — bridge the ring's signal line to one of the supported GPIOs.
- `--neopixel-count 32` — pixel count in the ring; 12/16/24/32 are common.
- `--no-neopixel` — force-disable; useful on hosts without the ring or when running unprivileged.

Dependencies on the camera host:

```bash
sudo apt install -y python3-pip
sudo pip3 install --break-system-packages adafruit-blinka adafruit-circuitpython-neopixel
```

The default `neopixel` library uses DMA/PWM and requires root. The `hil-camera.service` unit runs as `pi`; switch to `User=root` on hosts where the illuminator is in use, or use `neopixel_spi` on GPIO10 to stay unprivileged (not yet wired in this server).

## `/?full=1` snapshot

The warm-pipeline `/` snapshot serves from the largest full-FoV mode the sensor can sustain continuously (2328×1748 on the IMX519). For sensor-native resolution (4656×3496 on the IMX519) call `GET /?full=1` — the server briefly stops the video pipeline, configures a still capture at native resolution, takes one shot, then restores the video pipeline. Expect 1–2 seconds per call. AF / lens state is preserved across the reconfigure.

`--width 0 --height 0` (the default) means "use the camera's native resolution". For picamera2 backends the raw stream is always pinned to the sensor's full resolution so FoV stays full even when `--width/--height` are dialled down for lower bandwidth — without this, libcamera silently picks a centre-cropped sensor mode to match smaller main-stream sizes (the IMX519 at 1280×720 reads only the middle ~55%×41% of the active area).

## Architecture

Each backend opens the camera once at startup and runs a daemon grabber thread that writes the latest JPEG into a single-slot buffer. HTTP handlers serve from that buffer — no per-request camera open, no per-request AF cycle. AF runs continuously inside the camera's own pipeline (libcamera for Pi CSI, UVC controls for webcams), so snapshots stay sharp without explicit triggering.

The `/stream` endpoint avoids re-sending identical frames by waiting for a new timestamp before emitting the next multipart part.

## IMX519 autofocus footnote

Pi OS ships an `imx519.json` tuning file with no `rpi.af` algorithm block — but the libcamera IPA library does have the AF algorithm code compiled in. Symptom: `set_controls({"AfMode": Continuous})` emits `WARN IPARPI ipa_base.cpp: Could not set AF_MODE - no AF algorithm` and the lens never moves. `tuning/imx519-af-patch.py` adds an AF block (lens map `[0.0, 0, 32.0, 1023]`, matching the Arducam IMX519 16MP module) and continuous AF starts working immediately on next libcamera open. The patch is idempotent; the original tuning is saved as `imx519.json.preaf`.
