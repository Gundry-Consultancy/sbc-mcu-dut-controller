# HIL pipeline — handoff (2026-06-15)

Pick-up notes for the GitHub-CI hardware-in-the-loop (HIL) regression pipeline:
the `usbip-hil-controller` (this repo, on **tachyon** = `192.168.1.169`) tested
over Tailscale against the **rpi-displays** DUT host (a QT Py ESP32-S3,
`qtpy_esp32s3_n4r2`) from a WipperSnapper PR. Read with
[`hil-regression-pipeline.md`](hil-regression-pipeline.md),
[`api.md`](api.md), [`device-availability.md`](device-availability.md).

## TL;DR — what works (proven on real hardware 2026-06-14/15)

- **CI HIL pipeline on PR #930** (adafruit/Adafruit_Wippersnapper_Arduino,
  branch `hil-test-additions`): build → fetch firmware → flash → secrets →
  power-cycle → checkin/inject → **new PR comment per run** with inline proof +
  `hil-assets` artifact. Runs a **test array**:
  - **check-in smoke test** (default gate): `CHECKIN_VERDICT ok=true` ✅
  - **pixelWrite #926 regression** A/B: LOW `1.0.0-beta.127` → `rebooted=true`
    (crash) vs HIGH (PR fix) → `rebooted=false` (graceful) ✅
- **Controller hardening** (all deployed + live-validated): pipeline runs from
  `params` (no payload.kind); DB hardware overlay onto the topology device;
  `msc_filter` derived from the by-path serial; `power_cycle` awaits USB
  disappear→re-enumerate; reboot detection races serial-banner + MQTT; UTC-ms
  timestamped serial/flash/protomq logs; `verify_checkin` stage; **dwc_otg
  wedged-host auto-reboot recovery**.

## Where things are deployed

| what | location | commit |
|---|---|---|
| controller code | `main` of the controller repo, deployed to tachyon `~/dev-projects/python/usbip-hil-controller` | **ee7180e** |
| controller flag | `run/controller.env` on tachyon | `HIL_AUTO_HOST_REBOOT=true` (`HIL_HOST_REBOOT_ETA_S` defaults 300) |
| CI workflow + drivers | WS repo branch `hil-test-additions` (PR #930) | **4cbb1ccd** |

**Newer controller capabilities (deployed ee7180e, 2026-06-15 PM):**
- **On-demand DUT power** (`firmware_bench._power_on_dut`/`_power_off_dut`): only the
  job's solenoid channel is energised (at flash start, off at teardown). Idle DUTs —
  incl. a **bad/flaky board** — stay off the bus so one can't storm dwc_otg and wedge
  the hub. Steady state = all channels off; a job powers its own.
- **Gentle sequential recovery** (`reboot_host` with `channel_nodes`): all_off →
  reboot → bring up each channel **one at a time** (settle + presence + dmesg check)
  → off. Replaces the `all_on` storm that left the QT Py un-enumerated (validated by
  hand: one-at-a-time recovered it; the `-32` descriptor-read clears on a retry).
- **dmesg-aware presence probe** (`host_recovery.dmesg_usb_error_count` +
  `_node_present`, wired in `main.py`): healthy = by-path node present AND no real
  USB error storm (`-110/-71/-32/-62`, FSM-timeout, descriptor-read — *not* the
  benign "new … using dwc_otg" lines).
- **Full command output in CI logs** (`bench_stages.record`): the live event feed now
  carries every stdout+stderr line per command (UTC-ms stamped), not a whitelist —
  erase/flash progress + esptool deprecation warnings are visible live, not only in
  the flash.log asset.

Deploy = push controller `main` → on tachyon `git fetch && git merge --ff-only
origin/main && sudo systemctl restart hil-controller`. Bench access: SSH
`particle@192.168.1.169`, nested `ssh -i /etc/hil/keys/rpi-displays
pi@192.168.1.234`. Power scripts on the DUT host: `~/all_off.sh`, `~/all_on.sh`,
`~/turn_on.sh <ch>`, `~/turn_off.sh <ch>`.

## ⚠️ State at handoff

- **rpi-displays went fully offline ("No route to host") ~01:13Z** right after the
  check-in test passed — so the last pixelWrite A/B run (#8) failed `unknown`
  **only because the host was unreachable** (SSH `TimeoutError`/`No route` during
  firmware staging), **not** a logic bug. It self-recovers in **3–5 min**.
- **First moves in the new session:** confirm the bench is back, then start
  next-step #1 (tolerate a host reboot between test runs). Verify the bench:
  ```
  ssh particle@192.168.1.169 'ssh -i /etc/hil/keys/rpi-displays pi@192.168.1.234 "uptime; ls /dev/serial/by-id/ | grep -i qt"'
  curl -H "Authorization: Bearer $TOK" http://127.0.0.1:8080/v1/targets   # qtpy available?
  ```

## Next steps (priority order)

### 1. Tolerate a DUT-host reboot BETWEEN test runs  ← **DO FIRST**

**Goal.** One HIL CI job runs **3 firmware-bench jobs in sequence** (check-in,
pixelWrite LOW, pixelWrite HIGH). If a dwc_otg wedge triggers an auto-reboot of
the DUT host *mid-job*, the **remaining tests (and ideally the wedged one) must
WAIT for the host to come back (~3–5 min) and proceed** — not hard-fail. The
auto-reboot itself already works (validated); this makes the CI *ride through* it.

**Proven gap (run #8).** check-in passed → host went offline → LOW/HIGH errored
instantly with SSH `TimeoutError` / `No route to host` and reported `unknown`.
The workflow queries `/v1/targets` once at the start and never re-checks; the
drivers submit regardless and let the job error.

**Controller** (`host_recovery.py`, `firmware_bench`): ✅ **DONE** (commit pending).
- `mark_host_wedged` now sets, on the host's devices, `unavailable_kind=temporary`
  + **`retry_after = now + HIL_HOST_REBOOT_ETA_S`** (new env `HIL_HOST_REBOOT_ETA_S`,
  config `host_reboot_eta_s`, default 300 s) + reason "…reboot required, back ~Ns".
  `firmware_bench._flag_host_wedged` passes the configured ETA. `/v1/targets`
  returns `retry_after`. The `get_adapter` gate (reject a job on an unavailable
  device) is unchanged — the CI now treats that as *wait + retry*, not fail.
  Unit test: `test_mark_host_wedged_sets_retry_after_eta`.

**CI** (WS repo `hil-test-additions`): ✅ **DONE — local commit `c15e345b`, NOT
yet pushed to PR #930** (worktree `C:\dev\arduino\ws-hil-test-additions`). New
shared helper `.github/workflows/hil-lib.sh`:
- `wait_for_target_available <target>`: re-polls `GET /v1/targets`; `available` →
  proceed (echoes the fresh `device_id`); `temporary` → sleep until `retry_after`
  (+`HIL_WAIT_MARGIN_S` 15 s), re-poll, bounded by `HIL_WAIT_BUDGET_S` (360 s,
  covers the 3–5 min reboot); `permanent` → skip (rc 2); budget exceeded →
  timeout/fail (rc 3). **Called before submitting each test/side's job.**
- `is_host_offline_failure <events> <state>`: **reactive retry** classifier — a
  submitted job that errored with a host-offline signature (no route / SSH
  timeout / "device … unavailable" gate / wedge / blocked-by-lease) instead of a
  real verdict is treated as transient → wait + **re-submit that test once** (even
  the test that *triggered* the wedge). `run_target`/`run_side` expose verdict +
  terminal state via globals (the `$(...)` subshell otherwise dropped the state).
- **Report:** a waited/retried test notes "host rebooted — retried"; only a host
  still down past the budget marks the test skipped/failed w/ reason.
- **Unit tests:** `.github/workflows/hil-lib.test.sh` (stubbed curl/date/sleep,
  12 checks: available / temp-then-available / permanent / budget-exceeded + the
  classifier) — wired as a fast hardware-free CI gate before the ~95 min build-wait.

**Acceptance.** In a CI job where the host reboots after the check-in test, the
pixelWrite LOW/HIGH tests wait out the reboot and run to a *real* verdict; the run
comment shows all three with real results (no `unknown` from a transient host
outage).

**Run #10 (2026-06-15, SHA fc21185 / CI 094d587) — partial proof + a found gap.**
The wedge happened as expected and the **check-in test rode through it**: it
errored, waited 313 s for `retry_after`, the host auto-rebooted, the retry ran to
`ok=true ✅ (host rebooted — retried)` — §1's core mechanism works live. BUT the
pixelWrite jobs then errored with `[Errno 113] No route to host` (host still
L3-unreachable mid-reboot) and were reported `unknown` with **no** retry. Two
defects, both fixed in commits `a3c7071` (controller) + `4cbb1ccd` (CI), pending
re-run:
  1. **CI**: the wait loop broke on the state→terminal event *before* the
     error-reason event arrived, so the events log kept only the firmware-link
     line and the old signature classifier matched nothing. → now **drains
     trailing events** + retries on `is_infra_error(state)` (error/timeout/failed),
     not signature.
  2. **Controller**: a stage dying with a connection error wasn't a
     `HostUsbWedgedError`, so nothing flagged the device → `/v1/targets` kept
     showing it available → a retry would have nothing to wait on. → now
     `mark_host_unreachable` flags it temporary+`retry_after` (see §2), and a real
     presence probe self-heals it (see §3). ⏳ **Re-run pending** (run after `4cbb1ccd`).

### 2. Fully-down host (vs USB wedge) — ✅ flag + wait done (`a3c7071`)
The SSH-based auto-reboot (`host_recovery.reboot_host`) can only recover a host
**up but USB-wedged**. For an **L3-unreachable** host, `firmware_bench` now
classifies the connection error (`_is_host_unreachable_error`) and calls
`mark_host_unreachable` → devices flagged temporary + `retry_after`, host **not**
`reboot_required` (can't SSH-reboot an off-network box; it self-recovers and the
§3 probe clears it). **Still open:** out-of-band power recovery (smart plug / PoE /
solenoid hub) for a host that does *not* self-recover.

### 3. Presence probe — ✅ reachability done (`a3c7071`); dmesg storm still TODO
`availability_reconciler` now takes a real probe (wired in `main.py`): SSH to the
device's host and `test -e` its by-path node (10 s timeout), so the reconciler
**auto-clears** a temporary outage once the device re-enumerates (covers the new
unreachable flag AND a wedge that recovered out-of-band). **Still TODO** — the
*proactive* half: `dmesg` has no recent dwc_otg/USB error storm (`Timed out
waiting for FSM`, `-110`, reset/enumerate
storms). Use it to **auto-clear** a healed temporary outage AND to
**proactively** flag a flaky host *before* a full wedge. (User: "test dmesg
between and during runs for usb flakeyness.")

### 4. Full `pytest tests/` hangs
A test in `test_scheduler` / `test_firmware_bench` hangs the whole-suite run
(async teardown / "event loop closed"). Run targeted files meanwhile
(`test_host_recovery`, `test_bench_stages`, `test_solenoid_hub`,
`test_host_registry` all pass). Find + fix the hang so CI/`pytest tests/` is
usable again.

### 5. Smaller follow-ups
- **Lease overlap**: the next job can start while the prior job's
  `exclusive_device` lease is still active (window_minutes overlap) — seen as a
  "blocked by lease" warning; harmless but worth tightening (release lease at
  pipeline end, or have the next job wait on the lease).
- **`all_off` scoping**: `reboot_host` does `all_off` before the reboot (correct,
  after drain). Optional: an at-flag `all_off` scoped to *just* the wedged
  channel (don't yank other draining DUTs).
- **Re-enable nothing**: pixelWrite A/B is already enabled (not parked) — both
  tests run every PR.

## Live-test recipes

- **Wedge auto-recovery** (validated 2026-06-15 00:55Z): submit a firmware-bench
  job with `stages:[{enter_bootloader}]` while the QT Py is wedged → it raises
  `HostUsbWedgedError` → host flagged `reboot_required` → reconciler reboots
  (all_off→sudo reboot→all_on) → devices cleared → device back, ~2 min.
- **Full A/B**: push to `hil-test-additions` (triggers build + HIL on the
  `pull_request` path), or `gh run rerun <hil-run-id>` to reuse a build.

## Key memories (in `.claude/memory/`)

- `dwc-otg-wedge-hides-bench-reboot-fixes` — half the bench vanishing = reboot the
  Pi; maps taken during a wedge are unreliable; a `303a` boot blip is a Feather, not the QT Py.
- `project_firmware_bench` — the firmware-bench cycle + bench gotchas.
- `reference_rpi_displays_power`, `reference_hil_bench_usb_topology` — power/USB map.
- `feedback_never_filter_usb_by_vid`, `feedback_no_secrets_in_docs`.
