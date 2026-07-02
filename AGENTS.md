# sbc-mcu-dut-controller — agent orientation

FastAPI controller for a hardware-in-the-loop (HIL) bench fleet: it flashes and
drives real microcontroller + SBC DUTs over SSH-managed hosts, with solenoid
power control, camera proof capture, and I2C component-strand muxing. This is
the public, cleaned-secrets home of the platform (history shared with the
private `usbip-hil-controller` deploy repo).

## Start here

- Skills suite (how to *use* the platform): @.agent/skills/README.md
- HTTP API: `docs/api.md` — `GET /v1/targets`, `POST /v1/jobs`,
  `GET /v1/jobs/{id}/wait`, `GET /v1/jobs/{id}/assets`; bearer-token auth.
- Architecture: `docs/ARCHITECTURE.md`; platform tour: `docs/HIL_PLATFORM_OVERVIEW.md`.
- Job scripts: `firmware-bench` (flash+drive MCUs), `pytest-suite`
  (python-snapper SBC path); stage vocabulary in
  `src/hil_controller/adapters/bench_stages.py`.
- CI-callable job runner: `.github/workflows/example-hil-call.yml` + `examples/hil-call.sh`.

## Conventions

- Python 3.12, `ruff` + `mypy` + `pytest` (`pytest tests/`, asyncio auto mode).
- **No secrets in the repo** — example creds only (`bench-wifi` / `changeme` /
  `dev-token-change-me`); real values live in the deploy host's `run/controller.env`.
- Topology YAML seeds a runtime SQLite DB; live device status lives in the DB,
  not the YAML.
- App-specific skills (WipperSnapper Arduino/Python) are canonical here under
  `.agent/skills/` and duplicated into the firmware repos with a
  canonical-source header — edit here first.
