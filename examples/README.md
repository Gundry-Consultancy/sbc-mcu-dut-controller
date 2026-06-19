# Examples — caller-side HIL integration

Templates a downstream repository copies into its own CI to ask the
HIL controller to run a hardware test. None of these files contain
real credentials; they are skeletons for `${HIL_*}` substitution.

## Layout

```
examples/
  wippersnapper-arduino/
    secrets.example.json     # WS Arduino firmware secrets pointed at the bench ProtoMQ
  wippersnapper-python/
    secrets.example.json     # WS Python SBC client secrets
    .env.example             # pytest env loader, same fields
  hil-call.sh                 # reference CI step: submit job + long-poll
  ws-python-caller.yml        # WS-Python repo drops this in as .github/workflows/hil.yml

.github/workflows/
  example-hil-call.yml        # generic workflow_dispatch demo, both auth paths
  ws-python-ci.yml            # reusable workflow_call: pinned WS-Python flow
```

## Wippersnapper-Python "every commit" CI

This is the starter case: get HIL display tests running on every push
to the WS-Python repo. Two pieces:

- `.github/workflows/ws-python-ci.yml` (in *this* repo) — a reusable
  workflow that owns the job-submission body. Caller passes
  `repo_owner` / `repo_name` / `caller` (plus an optional `tests`
  pytest spec and `secrets_profile`); the workflow builds the
  `git-clone-and-run` job, pins it to the `wippersnapper-python`
  pool with `device.kind == "sbc"`, submits, and long-polls via the
  shared `examples/hil-call.sh`.
- `examples/ws-python-caller.yml` — the matching caller workflow the
  WS-Python repo drops in as `.github/workflows/hil.yml`. Triggers on
  `push` / `pull_request` for every-commit CI; `workflow_dispatch`
  is kept for ad-hoc runs that need a custom pytest spec or a
  different secrets profile.

The reusable workflow defaults the controller URL to
`http://wan.gdenu.fi:8080` and the OIDC audience to `hil-controller`,
so the WS-Python repo can drop the caller workflow in and start
pushing without setting any Actions vars first. Override
`HIL_API_BASE` / `HIL_OIDC_AUDIENCE` per-repo if you need a different
controller. If the controller's OIDC policy already covers the
repo (recommended), no `HIL_API_TOKEN` is needed either.

Default pytest spec is `tests/display`. Override by passing `tests:`
on `workflow_dispatch`, or change the default in
`.github/workflows/ws-python-ci.yml` once the WS-Python test layout
stabilises. Default setup command is `pip install -e .[test]`;
override with the `setup_command` input if WS-Python needs something
different (a JSON array of argv strings).

## Secrets substitution flow (firmware-binary path)

1. Caller stores real values as GitHub Actions secrets in their repo
   (`HIL_IO_USERNAME`, `HIL_IO_KEY`, `HIL_WIFI_SSID`,
   `HIL_WIFI_PASSWORD`, plus the per-repo HIL bearer token in
   `HIL_API_TOKEN` or — preferred — GitHub Actions OIDC).
2. CI step renders the chosen `secrets.example.json` with `envsubst`
   into the artifact directory, uploads alongside the firmware.
3. CI step calls `hil-call.sh` (or the equivalent in their language)
   pointing at the controller and the artifact.
4. Controller fetches the artifact, validates `sha256`, flashes the
   device, runs the requested test script, returns pass/fail.

The two `HIL_PROTOMQ_*` values are **not caller secrets** — they're
bench identity and the controller fills them in. Callers should leave
those as placeholders.

## Caller-side CI step

`hil-call.sh` is the reference flow a downstream repo can copy
verbatim. It submits a job and long-polls until terminal, exiting
non-zero on `fail` / `error` / `timeout` so the GitHub Actions job
fails the right way. See the top of the script for the env contract.

## Why two Wippersnapper variants

- **Arduino** (`Adafruit_Wippersnapper_Arduino`, this repo's
  `vendor/wippersnapper-arduino`) — microcontrollers (RP2040,
  ESP32-S2/S3, SAMD51, …). Flashed as a UF2 or via esptool.
- **Python** (currently private, not vendored — see
  `docs/ARCHITECTURE.md` §11.1) — single-board computers (Raspberry
  Pi family). Deployed as a Python package + `secrets.json`.

Both speak ProtoMQ; both accept the same `${HIL_PROTOMQ_HOST}` /
`${HIL_PROTOMQ_PORT}` placeholders in the caller-side workflow, but
render them into **different field names** inside the per-variant
`secrets.json` because the two firmwares parse different keys:

- Arduino — top-level `io_url` (string) and `io_port` (int), per
  `vendor/wippersnapper-arduino/src/provisioning/ConfigJson.cpp`.
  Defaults are `io.adafruit.com` and `8883`.
- Python — different keys, TBC once the (private) Python repo is
  reachable. The placeholder template in
  `wippersnapper-python/secrets.example.json` flags this and uses
  `mqtt_host` / `mqtt_port` as a TODO, not a stable contract.

The caller-side workflow doesn't need to know either set — it just
exports the `HIL_PROTOMQ_*` env vars and points `envsubst` at the
matching `secrets.example.json` for the variant being flashed.
