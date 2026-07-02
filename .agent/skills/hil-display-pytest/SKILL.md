---
name: hil-display-pytest
description: "Author/run a real-DISPLAY HIL test via the controller's pytest-suite (python-snapper) path — drive a real SPI/I2C panel on a python-snapper host (e.g. the Particle Tachyon localhost) end-to-end: real ProtoMQ broker + real WipperSnapper-Python client + real Blinka displayio, add display (splash)/write/remove cycles, with a camera ROI snapshot + log proof per stage. Use when adding/running a WipperSnapper-Python display test (ILI9341/ST7789/SSD1306/EPD...) on real hardware through the controller, wiring its topology device+peripheral+camera+ROI, and capturing run.log/protomq.log/camera images as job assets. NOT for flashing MCU firmware — that is the firmware-bench path (see hil-author-test); NOT for a two-build A/B (see hil-firmware-compare)."
---

# hil-display-pytest

How to run a **real display** test through the controller's non-interactive
`pytest-suite` (python-snapper) path. Unlike `firmware-bench` (flash an MCU,
drive stages, assert verdict lines), here the **device under test is the
controller/runner host itself** (or another python-snapper SBC): it runs the
WipperSnapper-Python client against a **real ProtoMQ broker** and drives a
**real Blinka `displayio` panel** over SPI/I2C. You assert on the captured
client log + a **camera ROI** of the panel.

Proven end-to-end on `tachyon-ili9341` (ILI9341 2.4" 240x320 on an EYESPI Pi
Beret, host `localhost`/transport `local`): add→splash, write, remove, re-add,
write, remove — `1 passed`, 17 proof assets.

## The shape of a run

1. **Topology** — the target device must exist with a camera + ROI:
   - device (e.g. `tachyon-ili9341`) under its host, `pool: wippersnapper-python`,
     `capabilities: [..., display-<driver>]`, `camera_id`, `peripheral_ids`.
   - a `peripheral` for the panel.
   - **`build_target` is operator-set in the DB, NOT topology-seeded** — set it
     directly (`update devices set build_target=…`) so `GET /v1/targets` carries
     a tag CI matches on. The seeder also will NOT delete peripherals you remove
     from topology — prune stale rows by hand.
   - calibrate the camera ROI: `PUT /v1/devices/{id}/camera/roi` with
     `{x,y,w,h,frame_width,frame_height}` (full-frame sensor coords). Verify with
     `GET /v1/devices/{id}/camera/snapshot?res=full`.
2. **Camera tune** (bright TFTs blow out auto-exposure) — apply a manual-sensor
   profile to the IP Webcam: `GET /settings/manual_sensor?set=on`,
   `/settings/iso?set=524`, `/settings/exposure_ns?set=7170511`. The test
   self-tunes via `WS_WEBCAM_*` envs, but persist a sane default.
3. **Submit** `POST /v1/jobs` with `script: "pytest-suite"` (below).
4. **Wait** `GET /v1/jobs/{id}/wait?since=&timeout=`; terminal on `finished|…`.
   `result: "pass"` ⇔ pytest exit 0.
5. **Pull proof** `GET /v1/jobs/{id}/assets` → `run.log` (client log),
   `protomq.log` (broker), and the per-stage camera + ROI images.

## The job body (pytest-suite + git-source)

```jsonc
{ "target": { "device": { "id": "tachyon-ili9341" }, "pool": "wippersnapper-python" },
  "script": "pytest-suite",
  "payload": { "kind": "git-source", "source": {
      "repo": "https://github.com/<owner>/Adafruit_Wippersnapper_Python.git",
      "ref": "<branch-or-sha>", "shallow": true, "submodules": false, "setup": [] } },
  "params": {
    "entry": "/home/particle/dev-projects/python/Adafruit_Wippersnapper_Python/.venv/bin/python",
    "args": ["-m","pytest","test/integration/display_test.py","-k","tachyon_webcam","-v","-s","-p","no:cacheprovider"],
    "extra_env": {
      "WS_REAL_DISPLAY_TEST": "1",            // opt into the gated real-HW test
      "WS_DISPLAY_ROI": "1611,251,608,768",   // crop just the panel from each frame
      "WS_DISPLAY_SNAPSHOT_DIR": "/home/particle/hil-proof/ili9341",
      "PROTOMQ_PATH": "/home/particle/dev-projects/python/Adafruit_Wippersnapper_Python/tools/protomq"
    },
    "collect_artifacts": [                    // harvested into downloadable assets
      "/home/particle/hil-proof/ili9341/*.jpg",
      "/home/particle/dev-projects/python/Adafruit_Wippersnapper_Python/tools/protomq/protomq.log"
    ]
  },
  "timeouts": { "total_s": 600, "deploy_s": 180, "run_s": 420, "flash_s": 60 } }
```

`collect_artifacts` (worker feature): a list of glob patterns the worker copies
into the job dir + registers as assets after the run. The worker also persists
the run stdout/stderr as **`run.log`**. Together: `run.log` + `protomq.log` +
images are all downloadable via `GET /v1/jobs/{id}/assets` (and CI-pullable).

## Why these exact knobs (gotchas that bite)

- **Use the bench's prebuilt venv as `entry`.** Blinka + `displayio` + `aiomqtt`
  live only in `…/Adafruit_Wippersnapper_Python/.venv` (board id
  `PARTICLE_TACHYON`); the system `python3` lacks them. The fresh git-source
  clone's package import resolves to that venv's editable install — fine when the
  branch differs from the persistent checkout only in the **test file** (loaded
  from the clone via the relative `test/...` path with `cwd=work_dir`). `setup`
  can be `[]` (deps already present).
- **The test launches its OWN broker** via the `protomq` fixture (`npm start` in
  `PROTOMQ_PATH`) — set `PROTOMQ_PATH` to a **built** protomq clone OUTSIDE the
  job workdir (submodules aren't cloned). Do NOT set `params.protomq` (that
  observer can't attach to a not-yet-started broker and just adds noise). `node`
  must be on the controller's systemd PATH (`/usr/bin/node`, not nvm-only).
- **`conftest` does `load_dotenv(override=True)`** — a `.env` overrides process
  env. Keep config in `extra_env` and ensure no stray `.env` in the workdir's
  parent chain. Leave `BLINKA_OS_AGNOSTIC` unset for real hardware (setting it
  forces host mocks).
- **Private repo clone:** `git_deploy` supports only a PAT-in-URL (`source.pat`),
  no credential helper. To clone a private fork without pasting a secret, give
  the runner user a github.com credential helper backed by an authed `gh`:
  `git config --global credential."https://github.com".helper '!sudo gh auth git-credential'`
  (root's active `gh` account must own/read the repo).
- **Bright panel → blown-out proof.** Apply the manual-sensor profile (above);
  the white-fill stage saturates regardless — read distinct stages (splash logo,
  text) rather than the fill.

## The test (in the repo under test)

A `WS_REAL_DISPLAY_TEST`-gated walkthrough in
`test/integration/display_test.py` driving the **real** `DisplayHardware` (no
`mock_display_hardware`) through `connected_client` + `send_and_receive_protobuf`:
add (splash paints) → write → remove → re-add → write → remove, asserting the
software state (`"<name>" in displays`, `driver`, `last_message`) and snapping the
camera per stage. Assert on `run.log` lines (`[real-tft]`, the broker B2D
`display.add/write/remove` publishes) + the ROI images, not the live stream
(pytest stdout arrives as one `run.log` at the end).

## Remote display on an SBC (controller-side capture)

When the panel is wired to a **remote** SBC (not the controller) — e.g. the 3.7"
UC8253 on `rpi-hil002` (Pi Zero W) or the 5.83" UC8179 on `rpi-hil004` (Pi 4) —
the split is: **the pytest runs ON the SBC** (drives the panel in-process = the
"basic client"), while **protoMQ + the webcam/proof run on the controller**. The
Zero W is ARMv6 and can't run the node broker at all, so this isn't optional.

Wiring it (proven green for UC8253 + UC8179):
- **protoMQ on the controller**: `params.protomq = {"launch_on": "controller"}`.
  The worker launches a broker locally (its own free ports) and injects
  `PROTOMQ_RUN_EXTERNALLY=1` + `MQTT_HOST/PORT` + `PROTOMQ_HOST/PORT/PATH` into
  the run env so the SBC client connects back. (The test's `protomq` fixture then
  prints "ProtoMQ run externally" and skips the local `npm start`.)
- **Controller-side capture**: `params.capture = {webcam_url, roi:[x,y,w,h],
  snapshot_dir, tune:{iso,exposure_ns}}`. The SBC test prints
  `WS_HIL_CAPTURE seq=N label=L kind=snap|splash window_s=W` stage markers; the
  worker streams the run's stdout line-by-line (transport `on_line` hook) and a
  `HilCapture` consumer turns each marker into a full frame + ROI crop under
  `snapshot_dir`. Harvest with `collect_artifacts: ["<snapshot_dir>/*.jpg"]`.
- **`entry`** = the SBC's venv python (e.g. `/home/pi/Adafruit_Wippersnapper_Python/.venv/bin/python`).
- The SBC needs the **same client stack** as the controller: `adafruit_epd` (the
  display driver — the *client's* job, install it on the SBC; PIL is NOT needed
  on the SBC, it lives on the controller where capture runs), plus the test deps.
- Point the SBC's persistent checkout at the test branch so the editable package
  matches the cloned test files (esp. the protoMQ external-mode handling).

Gotchas that bit (all now fixed in the controller, but watch for regressions):
- **SSH drops env vars.** `asyncssh env=` relies on sshd `AcceptEnv`; custom vars
  (`WS_REAL_DISPLAY_TEST`, the injected `MQTT_HOST`, …) silently never arrive and
  the test **skips** (hollow pass) or can't reach the broker. The SSH transport
  now inlines env as `KEY=val` in the command — verify with an `env | grep`
  diagnostic job if a remote test mysteriously skips.
- **The scheduler parses `request_json` twice** (once for the adapter, once for
  the worker) → `worker.params` and `adapter.params` are *different* dicts; the
  controller-protomq env injection writes to **both** so `GitDeploy.run` (reads
  the adapter's) actually sees it.
- **EPD config is the `config_epd` oneof.** Toggling `config_display.status_bar`
  on an EPD switches the proto oneof and wipes the mode/properties →
  "driver setup failed". Use `config_epd.properties.status_bar`.
- **Slow panels blow the round-trip timeout.** A real eInk add (splash +
  status-bar refresh) on a weak SBC exceeds the fixture's 5.5 s
  `send_and_receive_protobuf` timeout → `TimeoutError`. Raise it via
  `WS_PROTOBUF_TIMEOUT_S` (≈90 s Zero W / 150 s 5.83").
- **Don't run two eInk jobs concurrently.** Each controller-protomq exposes its
  API on the default port 5173; parallel jobs clash (`Cannot connect to
  192.168.1.169:5173`). Serialize them (matrix `max-parallel: 1`).
- eInk refresh flashes black mid-update, so the `splash` (kind=splash) frame can
  catch a dark transitional frame — the `after_add`/write frames are the reliable
  proof. Tune the splash window per panel (`WS_EPD_SPLASH_WINDOW_S`).

## CI

Commit `.github/workflows/hil-test-suite.yml` to the repo under test: it builds
this job body (repo/ref from the github context), submits via OIDC/`HIL_API_TOKEN`
to `HIL_API_BASE`, waits, then downloads `GET /v1/jobs/{id}/assets` and uploads
them as a proof artifact. Mirrors the Arduino PR #930 HIL intent for Python.

## Rules

- **Never simulate hardware.** A target that won't run is skipped + reported via
  `GET /v1/targets`, never faked.
- **Never paste secret values** into a committed file or commit message.
- Assert on the **finished** `run.log` + camera ROI, not prose or a racing stream.
- For MCU firmware flashing use `hil-author-test`; for A/B builds use
  `hil-firmware-compare`. This skill is display-on-a-python-snapper-host only.
