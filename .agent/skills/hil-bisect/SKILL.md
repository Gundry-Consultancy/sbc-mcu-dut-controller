---
name: hil-bisect
description: "Find the FIRST broken version in any monotonic series of firmware refs/releases with a boolean verdict criterion, by binary-searching between a known-good (working) and known-bad (broken) ref and flashing+testing each candidate on real HIL hardware via firmware-bench jobs (see hil-job-api). Drives hil_controller.bisect via scripts/hil_bisect.py, the controller web UI's bisect-run page, or a GitHub workflow_dispatch. Validates BOTH endpoints first (broken-also-passes -> fails 'criteria not specific enough'); a version that flashes but won't connect IS a valid 'broken' verdict; can't-flash/host-wedge is infra -> recover+retry, not a verdict; each version is tested twice. Worked example: WipperSnapper-Arduino releases (PyPortal Titano). NOT for a single A/B compare (hil-firmware-compare) or a one-off flash (hil-job-api)."
---

# hil-bisect

Binary-search a **monotonic series of firmware releases** to find the first one
where a given board stopped working, flashing + testing each candidate on real
hardware. Any repo whose releases ship a flashable asset works; the oracle is a
boolean verdict per version. Flagship worked case: **WipperSnapper-Arduino on
the PyPortal Titano** between `1.0.0-beta.78` (working) and `1.0.0-beta.128`
(broken) — 47 candidate releases, ~6 flash/test cycles.

## When to use
- "Which release broke <board>?" given a working and a broken ref.
- NOT a single before/after compare → that's **hil-firmware-compare**.
- NOT one ad-hoc flash → submit a **firmware-bench** job directly (**hil-job-api**).

## How it works (the engine: `hil_controller.bisect`)
1. **Enumerate** the repo's releases (GitHub API, `--repo` — default
   `adafruit/Adafruit_Wippersnapper_Arduino`), keep only those shipping a
   flashable asset for the board (`asset_glob`, fnmatch on asset names, e.g.
   `*pyportal_titano_tinyusb*.uf2`), sort by parsed version (`1.0.0-beta.78` →
   `(1,0,0,78)`; a **final release sorts above every beta** of the same x.y.z;
   unparseable tags dropped), and take the inclusive window between the two refs
   (direction is inferred — working may be newer or older than broken).
2. **Validate the oracle FIRST.** Flash+test both endpoints. Working MUST pass and
   broken MUST fail. If the **broken ref also passes → the run fails** with
   `"test criteria were not specific enough, both versions passed"` (+ logs). If
   the working ref fails → the oracle is invalid (test too strict / wrong ref).
3. **Bisect** the window: test the midpoint, move the good/bad bound, repeat until
   they're adjacent. The bad bound is the first broken release.

### Per-version verdict
Each candidate is one `firmware-bench` job (see **hil-job-api** for the stage
vocabulary; SAMD/UF2 default: `enter_bootloader(uf2-msc) → flash → power_cycle →
write_secrets_msc → power_cycle → verify_checkin{soft:true}`). The verdict is
read from the job logs:
- `CHECKIN_VERDICT ok=true` → **PASS** (flashed, booted, checked in to the broker).
- `CHECKIN_VERDICT ok=false` → **FAIL** — *broken but flashed*: booted-without-checkin
  **or didn't come up at all**. This is a real verdict; move on.
- job errored with **no** verdict line → **INFRA** (couldn't flash / host USB
  wedged) → recover + retry (`--infra-retries`, default 2); NOT a firmware verdict.

Each version is tested `verify_times` (default **2**) and the PASS/FAIL results
must agree (guards against a false detection on a flaky board/port); a
disagreement is surfaced as flaky for operator review.

### Adapting to a new series
The series is generic — any repo + `asset_glob` + flasher. The shipped oracle is
the broker check-in (`verify_checkin`); a different criterion means a different
per-version stage template. `--flasher` default is `uf2-msc` (copy the release
`.uf2` onto the bootloader drive); `bossac` (Adafruit fork) is the SAMD
alternative; ESP targets use `esptool` with their own stages — pass `--flasher`
+ a stage template. `DEVICE_ASSET_GLOB` in `scripts/hil_bisect.py` maps enrolled
device ids to default globs (extend it as boards are enrolled).

## Running it
CLI (controller URL + token + secrets from the env):

```bash
export HIL_BASE_URL=http://tachyon-<...>.ts.net:8080   # or http://127.0.0.1:8080 on the bench
export HIL_TOKEN=<controller bearer token>             # = HIL_STATIC_TOKEN
export IO_USERNAME=… IO_KEY=… WIFI_SSID=… WIFI_PASSWORD=…   # WiFi must reach the controller's protomq
export GITHUB_TOKEN=…                                   # optional, lifts GH API rate limits

python scripts/hil_bisect.py \
  --device mcu-pyportal \
  --working-ref 1.0.0-beta.78 \
  --broken-ref  1.0.0-beta.128 \
  --asset-glob '*pyportal_titano_tinyusb*.uf2'
```

Exit 0 + JSON `{first_broken, last_good, tested, window}` on success; exit 2 with a
message on a precondition failure (bad oracle / both-passed / unflashable target).

Other surfaces:
- **Controller web UI** — a bisection is a long-lived *orchestration* (it submits
  many child firmware-bench jobs), tracked in-process
  (`src/hil_controller/web/bisect_runs.py`), not in the job table; the result
  page polls the live log. Secrets are used only at runtime, never stored on the run.
- **GitHub `workflow_dispatch`** — `examples/wippersnapper-bisect/hil-version-bisect.yml`;
  drop it into the target repo's `.github/workflows/`.

## Broker / creds (worked example: WS-Arduino check-in oracle)
- `--io-url` picks the broker the DUT checks in to. **Blank = the controller's
  local per-session protomq broker**, which is *anonymous* — the runner derives a
  stable per-job identity, so IO creds can be placeholders. A **cloud** broker
  (`io.adafruit.com` / `.us`) needs a REAL `IO_USERNAME`/`IO_KEY` — the CLI
  warns and the check-in will FAIL otherwise.
- Placeholder `IO_KEY` values (`placeholder`, `your_aio_key_here`, …) are
  deliberately **dropped** before submit so the server anon-derives (local) or
  fails fast (cloud) instead of flashing a board that reboot-loops on an MQTT
  auth reject.

## Agent notes
- **Secrets**: WiFi must let the DUT reach the oracle broker (local protomq or
  cloud). Never commit secret values — pass via env / the controller's secrets
  profile.
- **Generous timeouts**: a flaky-port recovery round can take minutes; keep
  `--job-timeout-s` ≥ 900 and don't shorten the poll.
- The firmware-bench `target` is an **object** (`{"device":{"id":"…"}}`), not a
  string — see `hil_controller.bisect.BisectRunner._submit`.
