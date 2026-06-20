# WipperSnapper version-bisection

Find the first WipperSnapper-Arduino **release** where a board broke, by
binary-searching the releases between a known-good and known-bad ref and
flashing + connectivity-testing each on real HIL hardware. Engine:
[`hil_controller.bisect`](../../src/hil_controller/bisect.py); CLI:
[`scripts/hil_bisect.py`](../../scripts/hil_bisect.py); skill:
[`.claude/skills/hil-bisect`](../../.claude/skills/hil-bisect/SKILL.md).

## Surfaces
- **CLI / example script** — `scripts/hil_bisect.py` (below). Runnable now.
- **GitHub `workflow_dispatch`** (primary) — [`hil-version-bisect.yml`](hil-version-bisect.yml).
  Drop it into `Adafruit_Wippersnapper_Arduino/.github/workflows/`; run it from
  the Actions tab with the working/broken refs as inputs. See the header of that
  file for the required repo secrets/variables.
- **Controller UI job option** — planned follow-up.

## Quick start (CLI)

```bash
export HIL_BASE_URL=http://127.0.0.1:8080         # on the bench; or the Tailscale URL
export HIL_TOKEN=$(grep -oP 'HIL_STATIC_TOKEN=\K.*' run/controller.env | tr -d '"')
export WIFI_SSID=… WIFI_PASSWORD=…                # WiFi the DUT joins to reach protomq
export IO_USERNAME=hil IO_KEY=placeholder         # protomq autoresponds; creds can be dummy
export GITHUB_TOKEN=…                             # optional (GH API rate limits)

python scripts/hil_bisect.py \
  --device mcu-pyportal \
  --working-ref 1.0.0-beta.78 \
  --broken-ref  1.0.0-beta.128 \
  --asset-glob '*pyportal_titano_tinyusb*.uf2'
```

Output (stdout): a JSON `{first_broken, last_good, tested, window, …}`.
Exit `0` = boundary found; `2` = a precondition failed (the message says which:
bad oracle, both-versions-passed, or an unflashable target).

## What it guarantees
- **Oracle-validated**: both endpoints are flashed+tested first; if the "broken"
  ref also passes, the job fails with *"criteria were not specific enough"* + logs.
- **Verdict semantics**: a version that flashes but won't connect (or won't come
  up) is a **broken** verdict and the search moves on; a can't-flash / host-USB
  wedge is **infra** → recover + retry, never counted as a firmware verdict.
- **Verify twice**: each version is tested twice (configurable) and the results
  must agree.

## Notes
- SAM/SAMD51 boards flash via `uf2-msc` (copy the release `.uf2` onto the
  bootloader drive); the Adafruit-fork `bossac` is the alternative (`--flasher bossac`).
- A flaky-port recovery round can take minutes — keep `--job-timeout-s` generous.
- `--test-branch` / `--extra-cmd` are reserved for an optional extra test pass
  (pytest checkout / arbitrary command) layered on top of the connectivity gate.
