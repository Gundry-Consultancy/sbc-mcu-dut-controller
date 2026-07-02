# Copilot instructions

Read `AGENTS.md` at the repo root for orientation, and
`.agent/skills/README.md` for the HIL platform skills suite (how to submit
jobs, author bench tests, capture camera proof, and mux I2C strands).

Key facts: FastAPI controller (`src/hil_controller/`), bearer-token HTTP API
(`docs/api.md`), job scripts `firmware-bench` and `pytest-suite`, stage
vocabulary in `src/hil_controller/adapters/bench_stages.py`. Python 3.12 with
ruff + mypy + pytest. Never commit real credentials — example values only
(`bench-wifi`/`changeme`/`dev-token-change-me`).
