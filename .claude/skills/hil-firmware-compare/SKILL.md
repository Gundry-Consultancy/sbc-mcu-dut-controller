---
name: hil-firmware-compare
description: "General A/B firmware regression runner for the HIL controller. Use when you need to prove a behavioural difference between two firmware builds on real bench hardware — run the SAME flash+test pipeline (firmware-bench) on a low_ref and a high_ref, then assert an expected divergence in the captured logs/verdict. Drives the controller's GET /v1/targets availability matrix, POST /v1/jobs (script=firmware-bench), GET /v1/jobs/{id}/wait, and GET /v1/jobs/{id}/assets to fetch proof. The pixelWrite #927 crash-vs-graceful check is ONE example config — the runner is NOT hardcoded to it. Use for PR-vs-release firmware regression gating and posting the comparison as a PR comment. NOT for: a single one-off flash (use pico-hil-flash / a direct firmware-bench job)."
---

# hil-firmware-compare

Run the same flash+test pipeline on **two** firmware builds and assert they
**differ** the way you expect. This is a generic A/B harness over the HIL
controller's `firmware-bench` script — you supply the two refs, the targets, the
pipeline, and the assertion; it flashes each build on real hardware, captures
proof, and emits a comparison summary you can drop into a PR comment.

It is **not** tied to any one regression. The pixelWrite crash-vs-graceful case
is just the worked example in [Example config](#example-config-pixelwrite-927).

## Inputs (the comparison spec)

| input | meaning | default |
|---|---|---|
| `low_ref` | the "before" firmware. **CONTRACT: must be a published release** (e.g. tag `1.0.0-beta.127`). Sourced as the release's combined.bin. | — (required) |
| `high_ref` | the "after" firmware. **CONTRACT: must be the current PR's build artifact** (a CI build job's uploaded artifact). | — (required) |
| `targets` | build-job target names to run on, e.g. `qtpy_esp32s3_n4r2`. The `target` is the firmware-artifact name, mapped 1:1 to a bench device by `model`. | the single available board (today `qtpy_esp32s3_n4r2`); future callers pass the full matrix / a subset |
| `stages` | the `firmware-bench` stage pipeline run identically on both builds. | `enter_bootloader, erase, flash@0x0, power_cycle, write_secrets_msc, power_cycle, inject_pixelwrite` (a `power_cycle` MUST precede `write_secrets_msc` — the MSC volume only enumerates once the app boots) |
| `assertion` | the expected divergence over each run's log/verdict (low result ≠ high result, both as expected). | — (required; see example) |

### The low/high contract — and the fallback

The standard matrix is **only** valid for the published-release-vs-PR-artifact
pairing above:

- `low_ref` = a published release → its combined.bin (the release's fatfs ZIP).
- `high_ref` = the current PR → its build-job artifact's combined.bin.

If the caller asks for **any other combination** (PR-vs-PR, tag-vs-tag,
release-vs-local-file, two arbitrary commits, ...), do **not** silently run the
standard matrix. Tell the user this falls outside the standard matrix and fall
back to a **custom script run** — i.e. drive two ad-hoc `firmware-bench` jobs
with whatever firmware paths they provide and compare them, clearly labelled as
non-standard. Surface this in the summary so the gap is explicit.

## Controller API

- **Base URL:** `http://tachyon-16ee27b8.ostrich-escalator.ts.net:8080`
  (reachable over Tailscale). All `/v1` calls use **Bearer token** auth:
  `Authorization: Bearer $HIL_TOKEN`.

The runner only touches four endpoints:

1. `GET /v1/targets` — the availability matrix.
2. `POST /v1/jobs` — submit a `firmware-bench` run.
3. `GET /v1/jobs/{id}/wait` — block until the job reaches a terminal state.
4. `GET /v1/jobs/{id}/assets` + `GET /v1/jobs/{id}/assets/{asset_id}/download` —
   pull the proof (logs + boot version).

## Workflow

### 1. Resolve availability — `GET /v1/targets`

Request the matrix and reconcile it against `targets`. Each entry carries
`available`, `status`, `kind` (`temporary` | `permanent` | null), `reason`, and
(when temporary) `retry_after`. Decide per target:

- **available** → run the A/B comparison.
- **unavailable, kind=`temporary`** → the controller self-rectifies on its own
  (enumeration / hub-glitch heal). Give it the documented budget — **≤3 tries /
  ~3 min** — by polling `GET /v1/targets` until it flips to `available` or the
  budget is spent. If it heals, run; otherwise **skip + report** "temporary:
  &lt;reason&gt;".
- **unavailable, kind=`permanent`** → **skip immediately + report** "permanent:
  &lt;reason&gt;". Never retry — no retry can fix a board that isn't wired.

Skipped targets (either kind) **never fail the comparison** — they are listed in
the summary with reason + kind so the gap is visible. See
`docs/device-availability.md` for the self-rectification model (the
`HIL_AVAIL_RETRY_ATTEMPTS=3` / `HIL_AVAIL_RETRY_WINDOW_S=180` budget). Today every
DUT except `qtpy_esp32s3_n4r2` is permanently unavailable (bench offline), so the
matrix runs only the QT Py and reports the rest skipped.

### 2. Source the two firmware images

**low (release):** download the release's **fatfs ZIP** (it *contains* the
combined.bin) and unzip it:

```bash
gh release download "$LOW_REF" --repo "$FW_REPO" --pattern '*fatfs*.zip' --dir ./low
unzip -o ./low/*fatfs*.zip -d ./low
# -> ./low/...<target>...combined.bin
```

**high (PR artifact):** download the PR build job's artifact:

```bash
gh run download "$PR_RUN_ID" --repo "$FW_REPO" --name "$TARGET" --dir ./high
# -> ./high/...<target>...combined.bin
```

Each target has its own combined.bin; pick the one whose name matches the
`target`. All ESP32 combined.bins flash at offset `0x0`.

### 3. Run `firmware-bench` on each build — `POST /v1/jobs`

For each `(target, build)` submit one job. Same `params.stages` for both builds —
only `params.firmware.path` differs:

```bash
curl -sS -X POST "$BASE/v1/jobs" \
  -H "Authorization: Bearer $HIL_TOKEN" -H 'Content-Type: application/json' \
  -d '{
    "script": "firmware-bench",
    "target": "qtpy_esp32s3_n4r2",
    "params": {
      "firmware": { "path": "/abs/path/to/combined.bin", "offset": "0x0" },
      "stages": [
        {"type": "enter_bootloader"},
        {"type": "erase",  "before": "no_reset", "after": "no_reset"},
        {"type": "flash",  "offset": "0x0", "before": "no_reset", "after": "no_reset"},
        {"type": "power_cycle"},
        {"type": "write_secrets_msc"},
        {"type": "power_cycle"},
        {"type": "inject_pixelwrite"}
      ]
    },
    "secrets": {
      "IO_USERNAME": "...", "IO_KEY": "...",
      "WIFI_SSID": "...", "WIFI_PASSWORD": "..."
    }
  }'
```

Notes on the pipeline:
- `launch_protomq` stands up a per-session broker the freshly-flashed firmware
  connects back to (the controller auto-injects it after `erase` and a
  `start_serial_log` before the first `power_cycle` if you omit them; a
  `print_boot_log` is auto-appended after the last `power_cycle` to dump the FAT
  `*boot_out.txt`). `write_secrets_msc` writes `secrets.json` onto the MSC drive
  pointing at that broker.
- `inject_pixelwrite` fires a v1 `pixelWrite` at the checked-in DUT and logs a
  machine-greppable verdict line:
  **`PIXELWRITE_VERDICT rebooted=true|false ...`** (`true` = crashed/rebooted,
  `false` = handled gracefully). Reboot is detected by racing a **serial
  reset-banner** watcher against **MQTT re-checkin** (a crash shows in serial in
  ~1–2s, before the device can reconnect to re-checkin). It does **not** itself
  pass/fail on the reboot — the harness compares the two builds' verdicts.
- `msc_filter` is **auto-derived** from the device's by-path serial — don't pass
  it. The controller also supplies the protomq broker host:port.

> For authoring a *non*-A/B test (a check-in smoke test, a custom signal), see the
> **hil-author-test** skill and [`docs/api.md`](../../docs/api.md). The lightweight
> default gate is a `verify_checkin` stage (logs `CHECKIN_VERDICT ok=…`) instead of
> `inject_pixelwrite`.

**Never include secret values in this skill, memory, or any committed doc** —
read them from the environment / the controller's configured secrets at runtime.

### 4. Wait — `GET /v1/jobs/{id}/wait`

Poll/block on the wait endpoint until terminal (`finished` / `error` /
`timeout` / `cancelled`). A `firmware-bench` job is an interactive hold; for an
A/B run set a short window (it only needs flash + the inject stage, not the full
30-min default) so the job releases promptly.

### 5. Fetch proof — `GET /v1/jobs/{id}/assets`

List assets, then download the relevant ones via
`GET /v1/jobs/{id}/assets/{asset_id}/download`:

- **`serial.log`** — boot + runtime serial, incl. the `PIXELWRITE_VERDICT` line.
- **`protomq.log`** — broker side (checkin, the injected message).
- **`flash.log`** — full esptool transcript (chip id, MAC, erase, write, verify).
- the firmware **version** from the FAT `*boot_out.txt`, surfaced by the
  `print_boot_log` stage in the serial/bench log.

### 5b. (Optional) Capture the device display per build

For a display-driving DUT, grab a full-res ROI crop after each build's pipeline
settles so the A/B includes a visual diff, not just logs:

```bash
curl -H "Authorization: Bearer $TOK" \
  "$BASE/v1/devices/$DEVICE/camera/snapshot?res=full&pad=0.05" -o low.jpg   # and high.jpg
```

The ROI is frame-relative (`roi_frame_*`) so the crop is sharp at sensor res; one
shot per build (don't poll — heavy on weak Pis), taken once the screen has
refreshed. See [api.md](../../../docs/api.md#cameras--rois). Attach both crops to
the PR comment alongside the log evidence.

### 6. Evaluate the assertion & summarise

Apply the `assertion` to each build's captured logs/verdict. The comparison
**passes** iff both builds matched their expected side of the divergence.

Emit a summary suitable to **post as a PR comment**:

- a per-target verdict table: `target | low_ref result | high_ref result | pass/fail`;
- the **skipped/unavailable** targets with **reason + kind** (temporary/permanent);
- the firmware versions read from each build's `boot_out.txt`;
- links to (or inlined excerpts of) the `serial.log` / `protomq.log` / `flash.log`
  assets for each run as evidence.

## Example config (pixelWrite #927)

The canonical regression: a v1 `pixelWrite` to an uninitialised strand crashed
pre-fix builds and is handled gracefully post-fix (#927, beta.129+).

```
low_ref   = 1.0.0-beta.127            # published release (crash)
high_ref  = <current PR artifact>     # the fix (graceful)
targets   = [ qtpy_esp32s3_n4r2 ]
stages    = enter_bootloader, erase, flash@0x0, power_cycle,
            write_secrets_msc, power_cycle, inject_pixelwrite
assertion = low  build logs  PIXELWRITE_VERDICT rebooted=true   (crash)
            high build logs  PIXELWRITE_VERDICT rebooted=false  (graceful)
```

Pass iff the low build rebooted **and** the high build survived. Swap `stages`
and `assertion` to run a different regression with the same machinery — that is
the whole point of this skill.

## Agent notes

- **Never simulate hardware.** If a target won't run, classify it via
  `GET /v1/targets` (temporary → give the controller its 3-try/3-min heal budget;
  permanent → skip) — don't fake a verdict.
- **Hold the low/high contract.** Release-vs-PR-artifact only; anything else →
  warn + custom run, never the standard matrix silently.
- **Same `stages` on both builds** — the only intended difference is the firmware
  image. Differing pipelines invalidate the comparison.
- **No secrets in committed files.** Pass `IO_USERNAME` / `IO_KEY` / `WIFI_SSID`
  / `WIFI_PASSWORD` from the environment at submit time.
- ESP32 combined.bins flash at `0x0`; the `firmware-bench` flow into download mode
  is handled by `enter_bootloader` (1200-touch / power-cycle recovery) — you don't
  drive esptool directly here.
