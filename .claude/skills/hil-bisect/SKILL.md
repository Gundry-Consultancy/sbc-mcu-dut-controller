---
name: hil-bisect
description: "Find the first WipperSnapper-Arduino RELEASE where a board broke, by binary-searching the releases between a known-good (working) and known-bad (broken) ref and flashing+testing each on real HIL hardware via the controller's firmware-bench API. Drives hil_controller.bisect / scripts/hil_bisect.py. Validates BOTH endpoints first (broken-also-passes -> fails 'criteria not specific enough'); a version that flashes but won't connect is a valid 'broken' verdict; a can't-flash/host-wedge is infra -> recover+retry, not a verdict; each version is tested twice. NOT for a single A/B compare (use hil-firmware-compare) or a one-off flash (firmware-bench)."
---

# hil-bisect

Binary-search WipperSnapper-Arduino **releases** to find the first one where a
given board stopped working, flashing + connectivity-testing each candidate on
real hardware. The flagship case: the **PyPortal Titano** between
`1.0.0-beta.78` (working) and `1.0.0-beta.128` (broken) — 47 candidate releases,
~6 flash/test cycles.

## When to use
- "Which release broke <board>?" given a working and a broken ref.
- NOT a single before/after compare → that's **hil-firmware-compare**.
- NOT one ad-hoc flash → submit a **firmware-bench** job directly.

## How it works (the engine: `hil_controller.bisect`)
1. **Enumerate** the repo's releases (GitHub API), keep only those shipping a
   flashable asset for the board (`asset_glob`, e.g. `*pyportal_titano_tinyusb*.uf2`),
   sort by version, and take the inclusive window between the two refs (direction
   is inferred — working may be newer or older than broken).
2. **Validate the oracle FIRST.** Flash+test both endpoints. Working MUST pass and
   broken MUST fail. If the **broken ref also passes → the job fails** with
   `"test criteria were not specific enough, both versions passed"` (+ logs). If
   the working ref fails → the oracle is invalid (test too strict / wrong ref).
3. **Bisect** the window: test the midpoint, move the good/bad bound, repeat until
   they're adjacent. The bad bound is the first broken release.

### Per-version verdict
The per-version pipeline is a `firmware-bench` job (SAM/UF2 default stages:
`enter_bootloader(uf2-msc) → flash → power_cycle → write_secrets_msc →
power_cycle → verify_checkin{soft:true}`). The verdict is read from the logs:
- `CHECKIN_VERDICT ok=true` → **PASS** (flashed, booted, checked in to the broker).
- `CHECKIN_VERDICT ok=false` → **FAIL** — *broken but flashed*: booted-without-checkin
  **or didn't come up at all**. This is a real verdict; move on.
- job errored with **no** verdict line → **INFRA** (couldn't flash / host USB
  wedged) → recover + retry; NOT a firmware verdict.

Each version is tested `verify_times` (default **2**) and the PASS/FAIL results
must agree (guards against a false detection on a flaky board/port); a
disagreement is surfaced as flaky for operator review.

## Running it
CLI (the controller-repo example script). Secrets + controller come from the env:

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

Other surfaces: a **GitHub `workflow_dispatch`** (the primary trigger) lives at
`examples/wippersnapper-bisect/hil-version-bisect.yml` — drop it into the WS-Arduino
repo's `.github/workflows/`. A controller **UI job option** is a planned follow-up.

## Agent notes
- **Secrets**: WiFi must let the DUT reach the controller's per-session protomq
  broker (the checkin target); IO creds can be placeholders (protomq autoresponds).
  Never commit secret values — pass via env / the controller's secrets profile.
- **Generous timeouts**: a flaky-port recovery round can take minutes; keep
  `--job-timeout-s` ≥ 900 and don't shorten the poll.
- **Flasher**: SAM/SAMD51 default is `uf2-msc` (copy the release `.uf2` onto the
  bootloader drive); `bossac` (Adafruit fork) is the alternative. ESP targets use
  `esptool` with their own stages — pass `--flasher` + a stage template.
- The firmware-bench `target` is an **object** (`{"device":{"id":"…"}}`), not a
  string — see `hil_controller.bisect.BisectRunner._submit`.
