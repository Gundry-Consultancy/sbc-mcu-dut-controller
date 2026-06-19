# HIL firmware regression pipeline

End-to-end automated firmware regression testing on **real hardware**, driven
from a GitHub PR through the `usbip-hil-controller`. The flagship case proves
the WipperSnapper **#926/#927** fix: a v1 `ws.signal.pixelWrite` to an
uninitialised NeoPixel strand crashes release `1.0.0-beta.127` but is handled
gracefully by the fix. The machinery is **generic** — any A/B firmware
comparison with a stage pipeline + an assertion can reuse it.

> Sibling docs: [`device-availability.md`](device-availability.md) (target
> availability + self-rectification), [`ARCHITECTURE.md`](ARCHITECTURE.md)
> (controller overview). The firmware-bench design + bench gotchas live in the
> in-repo memory (`.claude/memory/project_firmware_bench.md`).

---

## 1. The end-to-end flow

```
GitHub PR ──"WipperSnapper Build CI" produces per-target artifacts
   │
   └─ workflow_run ─▶ hil-test-suite.yml  (.github on the WS repo)
                         │  joins Tailscale → reaches the controller
                         │  GET /v1/targets        → which boards are available
                         │  gh release/run download → release .zip + PR artifact
                         │  unzip → combined.bin    → POST /v1/firmware (upload)
                         ▼
                usbip-hil-controller (tachyon, FastAPI)
                         │  POST /v1/jobs  script=firmware-bench  (×2: low, high)
                         ▼
                  firmware-bench orchestrator
                         │  enter_bootloader → erase → flash → launch_protomq
                         │  → write_secrets_msc → power_cycle → inject_pixelwrite
                         ▼
              rpi-displays / rpi-hil006 (DUT hosts) ── ESP/RP2040 boards on a solenoid hub
                         │  esptool over USB-Serial/JTAG; protomq broker per job
                         │  (a 2nd DUT host, rpi-hil006 — Pi 4B/xhci — supplements the
                         │   fleet: same provisioning via scripts/setup-hil-host.sh, its
                         │   own CSI camera + solenoid hub; either host can serve a target)
                         ▼
                  verdict: PIXELWRITE_VERDICT rebooted=true|false
                         │  serial.log / protomq.log / flash.log → assets
                         ▼
   hil-test-suite ◀── GET /v1/jobs/{id}/assets + /wait events
   posts PR comment (per-target table + skipped targets) + uploads artifacts
```

**Contract:** *low_ref = a published release, high_ref = this PR's build.* Any
other combination routes to a custom script run rather than the standard matrix.

---

## 2. Components (all in this repo unless noted)

### 2.1 Signal injection — `adapters/ws_signal_inject.py` + the `inject_pixelwrite` stage

The crux: deliver a v1 pixelWrite to a checked-in device and observe crash vs
graceful, **without editing `vendor/protomq`**.

* **Encoding** — `encode_pixels_write(pin, color, type)` builds the nanopb
  `signal.v1.PixelsRequest{ req_pixels_write: PixelsWriteRequest{ pixels_type=
  NEOPIXEL, pixels_pin_data, pixels_color } }`. For (`D0`, 200) that's the exact
  11-byte payload `1a 09 08 01 12 02 44 30 18 c8 01`.
* **Injection** — `WsSignalInjector` connects to the per-session broker
  (aiomqtt), learns the `device_uid` + detects readiness (`pinConfigComplete`)
  on `<io_user>/wprsnpr/#`, then fires the write via protomq's **`POST /api/echo`**
  (`{topic, payload}`, payload latin1-encoded) to
  `<io_user>/wprsnpr/<uid>/signals/broker/pixel`. (`register_autoresponder()` is
  the queued-on-checkin alternative; protomq also exposes `/api/autoresponse`,
  `/api/scripts/:name/steps/:step/send`.)
* **Verdict** — after firing, it watches MQTT for a fresh re-checkin: a reboot
  within the window = the firmware crashed (release); silence = it survived (the
  fix). No serial-reader contention. The stage logs a machine-greppable
  `PIXELWRITE_VERDICT rebooted=true|false` and records the exact injection in
  `flash.log`. The stage does **not** pass/fail itself — the harness compares
  the two builds.

The regression's root cause (#927): the global `strands[]` was brace-initialised
for only element 0, leaving the rest with `pinNeoPixel == 0`. Pin `D0` → pin
**0** false-matches an uninitialised strand in `getStrandIdx`, bypassing the
`ERR_INVALID_STRAND` guard, so `fillStrand` calls `neoPixelPtr->fill()` on
`nullptr` → panic/reboot. The fix initialises every element to `-1`; the guard
then fires and logs `ERROR: Pixel strand not found, can not write a color to the
strand!`.

### 2.2 Device availability — `availability.py` + `availability_reconciler.py` + `GET /v1/targets`

So CI can request a matrix and get a truthful "ran these / skipped those, why".
DB-backed, **temporary** outages self-rectify (≤3 tries / ~3 min), **permanent**
ones never retry; both skip-and-report (never red). See
[`device-availability.md`](device-availability.md). Key implementation fact: the
topology seeder's device upsert (`ON CONFLICT … DO UPDATE`) does **not** touch
`status` / `unavailable_*` / `retry_*` / `build_target`, so DB-set availability
**survives re-seed**; `model` / `capabilities` / port fields stay
topology-authoritative.

### 2.3 `build_target` tag

`GET /v1/targets` keys each device off its `build_target` (the arduino-cli
platform name, e.g. `qtpy_esp32s3_n4r2`) so a CI matrix maps 1:1 to the build
job's artifacts; it falls back to the device model when unset.

### 2.4 Firmware delivery — `adapters/firmware_fetch.py` + `POST /v1/firmware`

firmware-bench copies `params.firmware.path` (a **controller-local** path) to the
bench. Two CI-friendly ways to get the `.bin` there:

* **URL** — `params.firmware.{url, sha256?}`: the controller downloads it
  (optional bearer token, sha256-verified). Good for public release assets.
* **Upload** — `POST /v1/firmware` (raw body + `?filename=`): stores the blob,
  records a `kind='firmware'` **asset** (job_id NULL + `purge_at`, default 7d via
  `HIL_FIRMWARE_PURGE_DAYS`), returns `{id, filename, path, size_bytes, sha256}`.
  Use this for PR build artifacts (not public URLs).

firmware-bench `_stage_firmware` **links** the firmware to the flashing job
(UPDATEs the uploaded asset's `job_id`, or registers one for a url-fetched path),
so firmware is tracked with jobs on the Assets page and purged with them.

### 2.5 Job assets API — `GET /v1/jobs/{id}/assets` + `/{asset_id}/download`

Lists/streams a job's captured `serial.log` / `protomq.log` / `flash.log` (+
firmware) so CI pulls proof without scraping the web UI. The version is read from
the FAT `*boot_out.txt` by the `print_boot_log` stage.

### 2.6 Agnostic skill — `.claude/skills/hil-firmware-compare`

An operator-usable A/B runner: `low_ref` + `high_ref`, a target list, a stage
pipeline + an assertion spec. The pixelWrite case is one config; swap
`stages`/`assertion` for any regression.

### 2.7 CI — `hil-test-suite.yml` + a test array (on the WS repo PR branch)

Triggers: `pull_request` (runs from the PR branch; waits for "WipperSnapper Build
CI" on the head SHA), `workflow_run` (post-merge), `workflow_dispatch`. Joins
Tailscale, validates the release-low/PR-high contract, queries `/v1/targets`,
fetches firmware, then runs a **test array** — each test is its own driver
script reported individually:

- `hil-checkin-run.sh` — **default gate**: flash PR build → secrets →
  power-cycle → `verify_checkin` (`CHECKIN_VERDICT ok=true`).
- `hil-pixelwrite-run.sh` — **pixelWrite regression (#926, fixed by #927)**: A/B
  LOW(release)=`rebooted=true` vs HIGH(PR)=`rebooted=false`.

Each driver appends a `### <name>` section + an inline serial.log excerpt + its
per-test assets to a shared `hil-out/comment.md`; the workflow writes the header
once and posts a **new comment per run** (run #/commit/links — not sticky). Each
driver waits for its job to be *terminal* before pulling only the `log`-kind
assets (serial/protomq/flash, not the firmware bin). Tests run `if: always()`
(one failure doesn't hide the others; the job still fails if any does). Add a
test = add a driver + a step — see the `hil-author-test` skill. Verdicts proven
on hardware 2026-06-14/15: LOW `beta.127` rebooted=true, HIGH PR-fix
rebooted=false, check-in ok=true.

> **Planned (next):** tolerate a DUT-host reboot *between* the test runs — the
> controller advertises `retry_after` (expected downtime) on a wedge/auto-reboot
> and each driver `wait_for_target_available` before submitting (+ one re-submit
> on a host-offline error), so a mid-job auto-recovery doesn't fail the remaining
> tests. See [`HANDOFF.md`](HANDOFF.md) §1.

---

## 3. Running it

On the WipperSnapper repo set:

| Secret | Purpose |
|---|---|
| `TAILSCALE_AUTHKEY_TYETH` | join the tailnet to reach the controller |
| `HIL_API_KEY_TYETH` | controller Bearer token |
| `HIL_IO_USERNAME` / `HIL_IO_KEY` | Adafruit IO creds for `secrets.json` |
| `HIL_WIFI_SSID` / `HIL_WIFI_PASSWORD` | DUT Wi-Fi |

| Var (optional) | Default |
|---|---|
| `HIL_API_BASE` | `http://tachyon-16ee27b8.ostrich-escalator.ts.net:8080` |
| `HIL_LOW_REF` | `1.0.0-beta.127` |
| `HIL_TARGETS` | `qtpy_esp32s3_n4r2` |

The **broker host:port is NOT a CI input** — the controller's `write_secrets_msc`
stage fills `io_url`/`io_port` from the per-session protomq it launches.

It runs automatically after the build workflow on a PR, or via `workflow_dispatch`
(`low_ref`, `high_ref`).

---

## 4. The bench inventory model

`build_target` maps a device to its build artifact. Today only the QT Py S3 is
enrolled:

| Device record | build_target | status |
|---|---|---|
| `mcu-feather-eink-29-rbw` (the live QT Py S3 on ch4 — historically mislabelled) | `qtpy_esp32s3_n4r2` | available |
| `mcu-qtpy-oled-091-stemma` (actually a QT Py **S2** duplicate) | `qtpy_esp32s2` | unavailable (permanent) |
| all other MCU DUTs | — | unavailable (permanent) — not enrolled |

`/v1/targets` reports this; the CI matrix runs only the available subset and
lists the rest as skipped. (Cosmetic follow-up: correct the kept record's model
label / id in `topology.yaml` — the `build_target` tag already drives CI.)

---

## 5. Extending

* **A different regression** — pass a different `stages` pipeline + assertion to
  the skill / workflow. The `inject_pixelwrite` stage is one stage among many in
  `STAGE_HANDLERS`; add new injection stages the same way (no orchestrator edit).
* **More boards** — enrol a device (set its `build_target`, bring it online),
  add the build-target to `HIL_TARGETS`. The controller reports availability;
  the matrix subsets automatically.
* **Other signals** — extend `ws_signal_inject` with more encoders (the v1
  `PixelsRequest` oneof also has create/delete; other components have their own
  signal messages).

---

## 6. Operational notes (bench)

* The DUT host (rpi-displays, Pi Zero 2 W) uses the legacy `dwc_otg` USB driver;
  `dwc2` makes a *failing* device cycle faster and is worse for flashing. Reboot
  clears a wedged `dwc_otg`; it is **not** runtime-rebindable.
* **ModemManager is masked** on the bench — it toggles DTR/RTS on USB-Serial/JTAG
  (= EN/IO0) and resets the chip. `scripts/setup-hil-host.sh` masks it.
* A blank-flash ESP32-S3 boot-loops (`invalid header` → TG0 watchdog ~2s); the
  bench enters download via the 1200-touch (app) or the USB-Serial/JTAG reset
  (`--before default_reset`, for the blank/boot-loop state). See the firmware-bench
  memory for the proven recipe.
