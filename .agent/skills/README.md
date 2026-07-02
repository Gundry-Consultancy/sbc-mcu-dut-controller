# HIL platform skills — canonical suite

This folder is the **source of truth** for agent skills that drive the
SBC/MCU DUT HIL test platform (this repo's controller, its benches, cameras,
solenoid power, and I2C strand muxing). `.claude/skills` is a symlink here, and
the agent entry files (`CLAUDE.md`, `GEMINI.md`, `AGENTS.md`,
`.github/copilot-instructions.md`) reference this README.

Two tiers:

- **Platform (generic)** — target-app agnostic. The platform flashes, powers,
  muxes, photographs and log-asserts *any* firmware/app; WipperSnapper appears
  only as a worked example.
- **App-specific** — WipperSnapper Arduino / WipperSnapper Python material.
  These are *duplicated into the firmware repos* so their contributors get them
  without cloning this repo; each copy carries a "canonical source" header
  pointing back here. Edit here first, then refresh the copies.

| Skill | Tier | Use for |
|---|---|---|
| `hil-job-api` | platform | **Start here.** The controller's HTTP job API: targets, firmware upload, job shape, event polling, assets, `target.requires`, the CI job-runner pattern. |
| `hil-author-test` | platform | Author a new flash/drive/assert test — proven stage order per chip family (ESP32 / RP2040 BOOTSEL / SAMD UF2), verdict contracts, no-flash pipelines. |
| `hil-firmware-compare` | platform | A/B two builds through the same pipeline and assert an expected divergence (PR-vs-release regression gating). |
| `hil-bisect` | platform | Binary-search any release series on real hardware for the first broken version (validates both endpoints; infra-vs-verdict discipline). |
| `hil-i2c-strands` | platform | Route a shared I2C component strand to one DUT (ADG729 + on-strand TCA9548), request components via `target.requires`, mux-preserving flashing (absorbs the old hil-mux-i2c-swap). |
| `hil-camera-proof` | platform | Capture visual proof as job assets: `capture_display` tuning, ROI calibration, focus drivers, camera-server deployment. |
| `hil-bench-recovery` | platform | Bench triage: availability semantics + frozen-flag recovery, solenoid/BOOTSEL control, host wedges, infra-error-vs-verdict discipline. |
| `hil-display-pytest` | platform | Real-display tests via the pytest-suite (python-snapper) job path with camera + log proof per stage. |
| `hil-display-arduino` | app: WS Arduino | Prove a WipperSnapper Arduino display on a real MCU: flash, v2 check-in, inject display Add/Write, camera proof to the PR. |

Related material elsewhere in this repo:

- `docs/api.md` — controller HTTP API (targets/jobs/wait/assets).
- `examples/hil-call.sh` + `.github/workflows/example-hil-call.yml` — the
  GitHub-Actions job-runner path downstream repos copy.
- `examples/wippersnapper-arduino/` — the WS-Arduino CI driver scripts
  (`hil-lib.sh` host-reboot resilience, `hil-checkin-run.sh` smoke gate,
  `hil-test-suite.yml` two-build workflow, `hil_lightup.py` no-flash tuning
  harness).
- `docs/notes/lilygo-tdisplay-camera-tuning.md` — the camera exposure/focus
  tuning journal behind the `capture_display` defaults.
