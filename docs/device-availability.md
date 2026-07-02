# Device availability & self-rectification

How the controller decides whether a requested target (board model / device) can
run *right now*, how it reports that to callers (CI matrices), and how it heals
transient failures on its own.

This exists because a HIL bench is not a static cloud fleet: a DUT's USB link
wedges, a hub browns out, a board is physically unplugged for a week, or a whole
class of boards simply isn't wired up yet. Callers (e.g. the
`hil-test-suite.yml` matrix on a firmware PR) want to *request a full target
matrix* and have the controller answer truthfully — "ran these, skipped those,
here's why" — instead of hanging on a port that will never enumerate.

## Model

Availability is **DB-backed** (authoritative in `devices`), so it survives
restarts, is queryable, and can be updated at runtime (by an operator, by a
job's outcome, or by the reconciler) without editing files. `run/topology.yaml`
remains the *seed* — it can set an initial `offline`/`reason`, but the live DB
wins (same precedence as the other runtime device fields).

Each device carries an availability triple (columns on `devices`):

| column | meaning |
|---|---|
| `status` | `available` \| `unavailable` (existing column, reused) |
| `unavailable_kind` | `temporary` \| `permanent` (NULL when available) |
| `unavailable_reason` | human string, e.g. `"USB enumeration wedged (error -110)"`, `"board not yet wired"` |
| `unavailable_since` | ISO8601 when it went unavailable |
| `retry_attempts` | self-rectify attempts spent on the current temporary outage |
| `retry_after` | ISO8601 earliest time to attempt rectification again (NULL = now) |
| `last_checked_at` | ISO8601 of the last presence/recovery probe |

`temporary` vs `permanent` is the key distinction:

- **temporary** — a transient fault the controller should *try to heal*: a
  wedged endpoint, a failed enumeration, a hub glitch. Eligible for
  self-rectification (below). Reported to callers as "skipped (temporary:
  &lt;reason&gt;), will retry".
- **permanent** — a standing fact no retry can fix: the board class isn't wired
  to the bench, the device was decommissioned, a known-dead port. Set
  deliberately (operator / topology). **Never retried.** Reported as "skipped
  (permanent: &lt;reason&gt;)".

## Self-rectification (temporary only)

A background reconciler periodically attempts to heal `temporary` outages:

- **Budget:** up to `AVAIL_RETRY_ATTEMPTS` (default **3**) attempts within
  `AVAIL_RETRY_WINDOW_S` (default **180 s**, i.e. "~3 minutes, 3 tries").
- **Cadence:** an attempt runs no sooner than `retry_after`; on each attempt the
  reconciler runs the device's **presence/recovery probe** (see below). On
  success → `status='available'`, clear the availability columns. On failure →
  `retry_attempts += 1`, set `retry_after = now + backoff`.
- **Steady cadence after the burst:** once `retry_attempts >=
  AVAIL_RETRY_ATTEMPTS` the device stays `unavailable/temporary` (it is *not*
  auto-promoted to `permanent` — permanence is a human/config decision) but it
  **keeps being re-probed on a slow schedule**: every
  `HIL_AVAIL_STEADY_RETRY_S` (default **900 s**). Spending the budget means
  *slower*, never *frozen* — a host that comes back an hour later heals without
  operator action. Set `HIL_AVAIL_STEADY_RETRY_S=0` to restore the old
  give-up-after-burst behaviour.
- **Operator retry-now:** `POST /v1/devices/{id}/availability/retry` (or the
  bulk `POST /v1/devices/availability/retry`, or the **Retry** button on the
  web UI devices page) zeroes the budget so the reconciler re-probes on its
  next tick. `permanent` devices are skipped/409 — edit the device instead.

The policy is a pure function (`availability.next_retry(...)` → `attempt now? /
wait until / give up`) so it is unit-tested without a bench; the reconciler is
the thin async wrapper that calls it on a timer.

### Presence probe

"Is this device actually here?" Cheapest first, escalating only as needed:

1. **Enumeration check** — does the device's `serial_port` / `hub_port_path`
   resolve on its host (the `/dev/serial/by-path` node exists)? This is the
   default `temporary→available` healer and matches how the bench already
   reasons about presence.
2. **Recovery action (optional, opt-in per device)** — for a known-wedgeable
   native-USB DUT, a bounded recovery (solenoid power-cycle → re-probe) may be
   attempted on the *last* retry before giving up. Off by default; gated so the
   reconciler never thrashes hardware.

Live presence can also be updated *opportunistically*: any job that flashes /
serial-captures a device reports success/failure back, nudging availability
without waiting for the reconciler tick.

## API

`GET /v1/targets` → the availability matrix callers consume:

```json
{
  "targets": [
    {"target": "qtpy_esp32s3_n4r2", "device_id": "mcu-qtpy-...", "host_id": "rpi-hil006",
     "available": true, "status": "available", "kind": null, "reason": null,
     "host": {"model": "Raspberry Pi 4 Model B", "cpu_cores": 4, "mem_total_kb": 4194304,
              "load1": 0.3, "speed_score": 8.1}},
    {"target": "feather_esp32s2", "device_id": "mcu-feather-...", "available": false,
     "status": "unavailable", "kind": "temporary",
     "reason": "USB enumeration wedged", "retry_after": "2026-06-14T16:20:00Z"},
    {"target": "metro_esp32s2", "device_id": "mcu-metro-...", "available": false,
     "status": "unavailable", "kind": "permanent", "reason": "not wired to bench"}
  ]
}
```

`target` is the **build-job target name** (the firmware artifact name), derived
from the device `model`, so a CI matrix can map 1:1 between "what the build job
produced" and "what the bench can run". Same Bearer auth as the rest of `/v1`.

Each record also carries its **host's auto-detected hardware** under `host`
(real board model, CPU/RAM/storage, live load, and a work-speed score) — see
`host_hardware.py`. Detection runs over the host transport (so a Pi Zero W no
longer reports the same static `model` as a Pi 5); an operator can pin any field
via the host edit form (`hw_override_json`, which wins on read). `speed_score` is
a work-speed multiplier vs an idle Pi Zero W (=1.0), refreshed only by the manual
"Benchmark speed" button. Host **and** device ids are renameable in the UI — the
rename cascades to every reference (`topology/rename.py`), so a board swap-out
doesn't orphan job history.

## How CI consumes it (the `hil-test-suite.yml` matrix)

1. The caller's build job produces artifacts named per target
   (`qtpy_esp32s3_n4r2`, ...). The HIL job `needs:` it.
2. The HIL job requests its matrix (today a single entry; in future the full set,
   users subsetting) and calls `GET /v1/targets`.
3. For each requested target:
   - **available** → run the A/B firmware comparison.
   - **temporary-unavailable** → the controller self-rectifies (≤3 tries / ~3
     min); if it heals in time, run; else **skip + report** "temporary:
     &lt;reason&gt;".
   - **permanent-unavailable** → **skip + report** "permanent: &lt;reason&gt;",
     no retry.
4. **Overall status passes if the available subset passes.** Skipped targets
   (temp or perm) never turn CI red — they are listed (with reason + kind) in the
   PR comment so the gap is visible without blocking the PR.

Today every DUT except `qtpy_esp32s3_n4r2` is marked unavailable (the rest of the
bench is offline), so the matrix runs only the QT Py and reports the others
skipped.

## Config knobs (env)

| env | default | meaning |
|---|---|---|
| `HIL_AVAIL_RETRY_ATTEMPTS` | `3` | self-rectify tries per temporary outage |
| `HIL_AVAIL_RETRY_WINDOW_S` | `180` | window the tries are spread across |
| `HIL_AVAIL_RECONCILE_S` | `30` | reconciler tick interval |
| `HIL_AVAIL_PROBE_RECOVERY` | `false` | allow a power-cycle recovery on the last retry |

## Status / scope

- **Implemented:** the DB columns (+ idempotent migration) and the `build_target`
  tag; the pure `availability` policy module (temp/perm classification + retry
  budget/backoff); `GET /v1/targets`; and the async reconciler
  (`availability_reconciler.py`, started in the app lifespan, guarded so
  headless/tests don't spin it). **Durability:** the topology seeder's device
  upsert (`ON CONFLICT … DO UPDATE`) does not touch `status` / `unavailable_*` /
  `retry_*` / `build_target`, so DB-set availability survives re-seed (verified
  live); `model` / `capabilities` / port fields stay topology-authoritative.
- **Wedged-host auto-recovery (dwc_otg):** when `enter_bootloader` exhausts all
  recovery *and* the by-path serial node is still absent, it raises
  `HostUsbWedgedError`; firmware-bench flags the host's devices
  `unavailable/temporary` + the host `reboot_required` (and the registry refuses
  new jobs on them). The reconciler then drains in-flight jobs and — gated by
  `HIL_AUTO_HOST_REBOOT` — reboots the host (`all_off → sudo reboot → wait →
  all_on`) and clears the devices. Validated live 2026-06-15. See
  `host_recovery.py` and the memory `dwc-otg-wedge-hides-bench-reboot-fixes`.
- **Next** (see [`HANDOFF.md`](HANDOFF.md) for the full plan):
  1. **Tolerate a host reboot between test runs** — a CI job runs 3 firmware-bench
     jobs in sequence; an auto-reboot mid-job must not hard-fail the rest.
     **Controller half done:** `mark_host_wedged` now sets `retry_after = now +
     HIL_HOST_REBOOT_ETA_S` (default 300 s) on the flagged devices and the reason
     notes "back ~Ns", so `/v1/targets` advertises the expected downtime.
     **CI half done (local commit `c15e345b`, not yet pushed to PR #930):** the
     shared `hil-lib.sh` `wait_for_target_available` (sleep until `retry_after`,
     bounded ~6 min) runs before each test + re-submit once on a host-offline
     error, so the remaining tests ride through the reboot. ⏳ pending live proof.
     (Last A/B run failed `unknown` *only* because the host went offline mid-run.)
  2. **dmesg presence probe** — the reconciler probe is still a stub (`False`);
     wire it to a by-path-node + `dmesg` USB-error-storm check (auto-clear healed
     outages; proactively flag a flaky host).
  3. **Fully-down host** — the SSH `reboot_host` can't recover an L3-unreachable
     host; distinguish "no route" (wait it out, ~3–5 min) from "up but wedged".
