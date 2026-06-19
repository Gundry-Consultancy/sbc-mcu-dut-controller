---
name: project-ws-python-display-hil
description: WipperSnapper-Python real-display HIL pipeline (tachyon-ili9341) via pytest-suite — proven green 2026-06-19
metadata:
  type: project
---

**✅ PROVEN GREEN 2026-06-19.** Real ILI9341 (2.4" 240x320, EYESPI Pi Beret,
SPI0 CS=D8/DC=D25/RST=D27) on the Particle Tachyon driven end-to-end through the
controller's **pytest-suite** path: real ProtoMQ broker → real WipperSnapper-Python
client → real Blinka `displayio`. `1 passed`, 17 downloadable proof assets
(run.log, protomq.log, 14 per-stage camera+ROI images). This is a DISTINCT
pipeline from the Arduino firmware-bench one ([[hil-ci-pipeline-state]]).

**Pieces (all committed + deployed):**
- **Topology** (`run/topology.yaml`, committed+synced): device `tachyon-ili9341`
  under host `localhost` (transport local, python-snapper), `pool:
  wippersnapper-python`, caps incl `display-ili9341`, `camera_id: android-note9`,
  peripheral `periph-tft-ili9341-24`. `build_target` (`tachyon-ili9341`) is
  **operator-set in the DB** (NOT topology-seeded) — that is the `/v1/targets`
  availability tag CI matches. Camera `android-note9` recorded `host_id:
  rpi-hil002` (the bench that "has" the phone webcam).
- **Camera**: `android-note9` (IP Webcam, 192.168.1.249:8080, 3840x2160) frames
  the Tachyon TFT. Bright-TFT manual-sensor profile: `manual_sensor=on iso=524
  exposure_ns=7170511` (else the lit panel blows out). ROI for tachyon-ili9341 =
  `1611,251,608,768` (full-frame coords); set via `PUT
  /v1/devices/{id}/camera/roi`, crop verified via
  `GET /v1/devices/{id}/camera/snapshot?res=full`.
- **Test**: `Adafruit_Wippersnapper_Python` branch **`hil-test-suite`** (off the
  splash branch `add-display-splash-image`, on the `tyeth` fork). Enhanced
  `test_display_real_ili9341_240x320_tachyon_webcam` (test/integration/display_test.py):
  WS_REAL_DISPLAY_TEST-gated, self-tunes the webcam (`WS_WEBCAM_*`), captures a
  baseline + per-stage full frame + `*_roi` crop (`WS_DISPLAY_ROI`, Pillow). Added
  Pillow to deps. Drives the REAL DisplayHardware (no mock).
- **Worker feature** (controller `worker.py`): `params.collect_artifacts` (glob
  list) → harvested into job assets after run; run stdout/stderr persisted as
  `run.log`. So run.log/protomq.log/images are all `GET /v1/jobs/{id}/assets`.
  +tests `tests/test_artifact_harvest.py`.
- **CI**: `.github/workflows/hil-test-suite.yml` in the WS-Python repo submits the
  job (repo/ref from github), waits, downloads assets → `hil-ili9341-proof`
  artifact. Mirrors PR #930 Arduino intent.
- **Skills**: controller `.claude/skills/hil-display-pytest` (the authoring guide,
  sibling to [[hil-author-test]]); WS-Python `.claude/skills/run-display-hil-test`.

**Job recipe (pytest-suite):** target device id `tachyon-ili9341`; `entry` = the
bench venv python `…/Adafruit_Wippersnapper_Python/.venv/bin/python` (Blinka/board
detection lives there; system python3 lacks it); `args` = `-m pytest
test/integration/display_test.py -k tachyon_webcam -v -s`; `extra_env`
WS_REAL_DISPLAY_TEST/WS_DISPLAY_ROI/WS_DISPLAY_SNAPSHOT_DIR/PROTOMQ_PATH;
`collect_artifacts` globs the snapshot dir + protomq.log; `setup: []` (deps in
venv). Job submission body saved patterns in [[hil-display-pytest]].

**Gotchas that bit (now solved):**
- **Broker is launched BY THE TEST** (the `protomq` fixture runs `npm start` in
  `PROTOMQ_PATH`), not the controller. So `PROTOMQ_PATH` must point at a BUILT
  protomq clone OUTSIDE the fresh git-source workdir (submodules aren't cloned);
  the persistent `…/tools/protomq` is built. Do NOT set `params.protomq` (its
  observer can't attach to a not-yet-started broker). `node` is `/usr/bin/node`
  (on the systemd PATH; not nvm-only).
- **`conftest` does `load_dotenv(override=True)`** — keep config in `extra_env`,
  ensure no stray `.env` up the workdir chain, leave BLINKA_OS_AGNOSTIC unset for
  real HW.
- **Private-repo clone:** `git_deploy` only supports `source.pat`, no helper. Set
  `particle`'s git helper once: `git config --global
  credential."https://github.com".helper '!sudo gh auth git-credential'` (bench
  root `gh` has the `tyeth` account active = owns the fork). See [[reference-bench-host]].
- Remove stage leaves the panel WHITE (display object released, backlight on),
  not black — the test asserts software state, not pixels, so it still passes.
