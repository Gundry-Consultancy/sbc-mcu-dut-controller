---
name: project-ws-bisection-harness
description: Spec for the WipperSnapper-Arduino version-bisection CI job (find the release where a board broke) — requirements as the user stated them
metadata:
  type: project
---

Planned feature (spec'd 2026-06-20, not yet built). A manually-run CI job that
bisects WipperSnapper-Arduino **releases** (commits eventually) to find where a
given `board_id` broke.

**Inputs:** `board_id`; two refs (tags/branches/SHAs) — one labelled **working**,
one **broken** (order can be either direction); optional test-branch to check out
for pytests; optional extra test command/script. Connectivity is the baseline
test ("does it come up + connect"), plus anything else requested.

**Enumerate between:** find the releases (ideally just releases, to keep the
candidate set small initially; commits later) between the two refs. WS publishes a
per-board UF2 per release, e.g. `wippersnapper.pyportal_titano_tinyusb.<ver>.uf2`.

**Oracle validation FIRST (do not skip):** flash + test BOTH endpoint refs before
bisecting. Expected: working ref PASSES, broken ref FAILS.
- If the **broken ref also PASSES** → **fail the job** with a report: "test
  criteria were not specific enough, both versions passed" + attach logs. (Same
  spirit if the working ref fails — the oracle is wrong; don't bisect on a bad
  oracle.)

**Failure-as-signal (key):** a version that flashes but then **fails to come up /
enumerate / connect IS a valid "broken" verdict** — record it and move on to the
next candidate. Distinguish this from **infrastructure failure** (can't flash at
all / host USB wedged): that is NOT a firmware verdict — it triggers recovery
(power-cycle the DUT; if the host USB stack is wedged, host-reboot — rpi-displays
is a Pi Zero 2 W on dwc_otg, see [[project-qtpy-dut-down]]) and a retry, not a
"broken" mark.

**Then bisect** (binary search over the release list) until the first failing
release is found for that board.

**Flasher VALIDATED 2026-06-20** on the real Titano (2/2 firmware-bench jobs
`finished pass`, flashing v78 via uf2-msc): mount `/dev/sda` (PORTALBOOT) → cp the
image → sync → board boots the WS app (the **`WIPPER` MSC drive enumerates with
`/secrets.json /boot_out.txt /code.py`** — so `write_secrets_msc`+`verify_checkin`
IS the connectivity test path for SAM, same as ESP32). The flaky port 1.1.4 was
beatable in software: the **catch-and-touch** entry (tight `sleep 0.2` poll for the
serial node, 1200-touch the instant it appears) + 3-tier recovery catches the
flapping/reboot-looping board. Recovery can take >7 min on a bad round, so the
bisection must use **generous per-job timeouts** (don't cap the poll at ~400s).
Release candidate set for the Titano v78→v128 = 47 releases (only beta.112 lacks
the asset, auto-skip) → ~6 flash/test cycles. WS release asset for the Titano:
`wippersnapper.pyportal_titano_tinyusb.<ver>.uf2`.

**BUILT + deployed 2026-06-20** (engine `hil_controller.bisect`, CLI
`scripts/hil_bisect.py`, skill `hil-bisect`, GH workflow_dispatch in
`examples/wippersnapper-bisect/`, and a controller **web UI**: `/ui/jobs/new-bisect`
→ POST runs `BisectRunner` on a worker thread (`web/bisect_runs.py`), `/ui/bisect/{id}`
HTMX-streams the log; secrets pulled from config `HIL_BENCH_WIFI_*`/`HIL_BENCH_IO_*`
in controller.env — never in the form). **Verdict reads the job EVENT stream**
(`GET /v1/jobs/{id}/wait`, paging by `since`), NOT the assets — `CHECKIN_VERDICT
ok=…` is a bench `log_line` event, not command output.

**OPEN BLOCKER for a live run (2026-06-20) — precisely diagnosed.** A v78
connectivity job (flash+secrets+verify_checkin) showed the whole chain works
EXCEPT WiFi association: flash ✓, boot ✓, `write_secrets_msc` ✓, **protomq
launched per-job** by the `launch_protomq` stage (NOT a standing service — MQTT
on 192.168.1.169:1884, the DUT's secrets point there) ✓, AirLift **"SSID found!"**
✓ — but then **stuck "Connecting to WiFi (attempt #0)"**, reboot-loops, never
associates → `CHECKIN_VERDICT ok=false`. The "Invalid IO credentials" lines were
the PRE-write default-secrets boot (expected; WS `fsHalt`s only when aio_user/key
== `YOUR_IO_*_HERE`), a red herring. So: the Titano's **AirLift ESP32
co-processor** can't join the (admittedly flaky) `bench-wifi` WiFi. Plan per the
user: (1) give it patience — flaky WiFi, 3 attempts/reboot, so use a long
`verify_checkin checkin_timeout_s` (≥300) for many retry cycles; (2) if still
failing, verify the AirLift **nina-fw version** (WS prints it on init); (3) last
resort, flash Adafruit's latest AirLift ESP32 firmware via the **PyPortal Titano
pass-through UF2**. The bisection oracle + flash path are sound; this is purely
the AirLift WiFi link. (PR #930 never exercised AirLift — those were native-WiFi
ESP32 boards.)

**Flasher reliability fixes (2026-06-20, deployed):** (a) `Uf2MscFlasher.flash`
re-enters the bootloader on a failed mount round (the UF2 bootloader auto-boots the
app after a slow `launch_protomq`, so the drive vanishes by mount time); (b)
**`_locate_msc` now reads the FAT label and accepts ONLY the `*BOOT` bootloader
drive, never the running app's `WIPPER` drive** — both are MSC at the same USB
by-path, and copying a .uf2 to WIPPER is a silent no-op (this is why "flashing"
beta.130 left boot_out showing beta.78). With both fixes the 3-stage flash is
reliable; the **6-stage connectivity pipeline still intermittently fails on the
flaky Pi Zero port 1.1.4** in different spots (‑110 storms / MSC-volume-never-
enumerates after a post-flash reboot — CDC half comes up at 8053 but the MSC half
doesn't). The Pi Zero 2W dwc_otg + marginal port can't reliably sustain it.

**CONFIRMED root cause (2026-06-20): port 1.1.4 `-110` storm.** Attempting the
AirLift nina-fw passthrough update, the device went fully off-bus — `device
descriptor read/64, error -110`, `device not accepting address`, device numbers
climbing (no vid:pid, no CDC, no MSC). Port 1.1.4 has a **documented `-110` /
"maybe the USB cable is bad?" history** (it was the flaky Feather slot before the
Titano was seated there, see [[reference-hil-bench-usb-topology]]). So the MSC-
never-enumerates + association failures all trace to a **marginal port/cable** —
an electrical fault no software can fix. The nina-fw update is BLOCKED by it too
(needs the bootloader MSC to copy the passthrough UF2). nina-fw artifacts for when
the port works: passthrough UF2 =
`cdn-learn…/PyPortal_M4_ESP_32_Passthrough_TinyUSB_2023_07_30.uf2`, firmware =
`adafruit/nina-fw` 3.3.0 `NINA_ADAFRUIT-esp32-3.3.0.bin`, flash via
`python3 -m esptool --port <tty> --before no_reset --baud 115200 write_flash 0x0 <bin>`.

**ACTUAL root cause (2026-06-21): a firmware BOOTLOOP, not a cable.** (The earlier
"cable fault" call was WRONG — corrected by the user.) The Titano is on
**rpi-hil006** (Pi4, xhci) port **1-1.2.1.4 = solenoid ch3** (mapped via dmesg —
sysfs never stabilises during the storm). The `-110`/`-22` storm is the SYMPTOM
of an app crashing/resetting faster than USB enumeration completes (enumerate →
`USB disconnect` → re-enum → -110, device numbers climbing). **The fix is the
tight 1200-touch HAMMER** (now `Uf2MscFlasher._catch_and_touch`): a continuous
~50ms host-side loop that touches the CDC the instant it appears in the boot
window + detects the *BOOT drive — **caught the Titano in ~6s**, dropped it into
the stable PORTALBOOT bootloader, flashed beta.78, and it now boots stably (8053,
with the normal ~30s no-secrets reboot). `samd51_uf2`'s POWER double-tap is
USELESS for this (RAM magic cleared by power-off) — removed from the recovery.
Device re-provisioned: host_id=hub_host_id=rpi-hil006, hub_port_path=1-1.2.1.4,
ch3, serial_port `…pcie…-usb-0:1.2.1.4:1.0`, camera csi-rpi-hil006.

**CONNECTIVITY SOLVED (2026-06-21) — Titano uses the io.adafruit.com CLOUD, not
local protomq.** After nina-fw 3.3.0 the AirLift associates with `bench-wifi`
("Connected to WiFi!"), but the **local protomq (aedes, strict MQTT) REJECTS the
AirLift CONNECT** ("Invalid header flag bits, must be 0x0") — never checks in
locally. Pointed at **io.adafruit.com:8883 (TLS)** with the public
**`playground_example`** account (key `aio_REDACTEDEXAMPLEKEY000000EXMP`, non-secret
demo creds) → full register: serial `Connected to WiFi! → Connecting to AIO MQTT →
Registration and configuration complete!`. So the oracle is **serial-based**:
`verify_checkin` gained `via: serial` (watch serial.log for "Registration and
configuration complete"); `bisect.default_stages` target io.adafruit.com:8883 +
serial checkin; firmware_bench skips launch_protomq when write_secrets_msc has an
external io_url. Bench WiFi: `bench-wifi`/`changeme` (controller is also on it). All
deployed; **live v78→v128 bisection running** (verify ×2). (The channel probe reads sysfs nodes, which never stabilise during
the storm → "not found"; dmesg is the source of truth here.) FIX = **swap the USB
cable** (known-good data cable); if it still storms, the board's USB connector is
damaged. Once it enumerates stably: map its solenoid channel (powers 1-1.2.1.4),
set host_id/hub_host_id=rpi-hil006 + the new by-path
`platform-fd500000.pcie-…-usb-0:1.2.1.4:1.0`, then resume nina-fw + bisection.

**ERASE + SECRETS fixes (2026-06-22, deployed `449cbed`).** Two bugs made an
earlier v78→v128 oracle lie/fail:
1. **Stale-firmware false PASS** — `Uf2MscFlasher.erase()` was a no-op and the
   bisection pipeline had NO erase stage, so a flash that silently didn't take
   left the PREVIOUS firmware booting + "passing". FIX (`6e84bd4`): real
   `erase()` = `bossac --erase --offset=0x4000` over the UF2 bootloader's SAM-BA
   **CDC** (composite CDC+MSC; `--erase` doesn't reset, so the `*BOOT` drive
   stays up for the copy); an `erase` stage now runs before `flash` in
   `default_stages` + `SAMD51_FLASH_STAGES`. A blanked app → a no-op flash drops
   to the bootloader (clear FAIL), never stale firmware. (v128-misreports-as-v127
   is a known firmware quirk, NOT a flash failure — ignore it.)
2. **Placeholder IO creds** — the web route force-passed `cfg.bench_io_username/
   _key` = the controller.env defaults **`hil`/`placeholder`**. So the DUT joined
   WiFi but the io.adafruit.com **MQTT auth was rejected** → `WDT RESET` reboot
   loop → no checkin → a perfectly-flashed v78 looked "broken" and the oracle
   aborted ("working ref did NOT pass"). Root-caused from the job event stream
   (`{io_username:"hil", io_key:"placeholder"}` in the written secrets.json).
   FIX (`449cbed`): **secrets come from the request, never hardcoded.** Local
   protomq is anonymous → `firmware_bench.anon_io_cred(job_id)` derives
   io_user=io_key from the 16-hex job id when a local write_secrets has no real
   creds; a CLOUD `io_url` needs a REAL account, enforced at submit by
   `bisect._check_secrets()` (`is_cloud_broker`/`is_real_io_key`) — fails fast
   instead of reboot-looping. `BisectConfig` gained `io_url`/`io_port`; the web
   form + CLI (`--io-url/--io-port`, `IO_*` env) take optional creds/broker;
   WiFi defaults `bench-wifi`/`changeme`. Pass the playground creds at RUN TIME
   (env / UI fields), not in controller.env. **Re-run after deploy: v78→v130,
   cloud, real playground creds via env.**

**(superseded) earlier recommendation:** move to the Pi4
(xhci, none of the Zero's wedge/flaky-USB issues) for the bisection, and sort the
**AirLift WiFi** independently (beta.78 confirmed can't associate with `bench-wifi`
— SSID found, stuck "Connecting attempt #0"; needs nina-fw check via passthrough
UF2 and/or AP-creds confirmation). The bisection infra is COMPLETE + proven where
the hardware cooperates; the only blockers left are this host's port + AirLift WiFi.

**firmware-bench job API (live shape, 2026-06-20 — the hil-firmware-compare skill
doc is STALE):** `POST /v1/jobs` body = `{"target":{"device":{"id":"<dev>"},
"pool":"public"},"script":"firmware-bench","params":{"firmware":{"url":"<asset>"},
"window_minutes":N,"stages":[...]},"secrets":{...}}`. `target` is an OBJECT
(`target.device.id`/`.model`), NOT a string. Poll `GET /v1/jobs/{id}` for
state/result (terminal: finished/error/timeout/cancelled); `/wait` long-polls with
a cursor (returns early, not block-to-terminal). Assets: `GET /v1/jobs/{id}/assets`
then `/assets/{aid}/download` (the downloaded firmware is saved bench-side as
`firmware.bin` regardless of .uf2 extension — UF2 bootloader validates by content).

**Real target case:** PyPortal Titano (`mcu-pyportal`, build_target
`adafruit_pyportal_m4_titano`), bisecting **v78 (working) → v128 (broken)** —
beta.128 onwards is the suspected-broken range (so beta.130 failing to come up is
consistent with the bug, not only the flaky port).

**Flasher reality** (see [[project-samd51-bossac-flasher]]): **Debian `bossa-cli`
1.9.1 is BROKEN for SAMD51 writes** (writeBuffer→"SAM-BA operation failed");
proven 2026-06-20. Use the **Adafruit/Arduino-fork bossac** instead (add it to
`setup-hil-host.sh`, prefer it over the apt one). The **UF2-MSC copy** path also
works and matches the `.uf2` release artifacts directly (proven: copy to the
`PORTALBOOT` drive → "device firmware changed"). 1200-baud double-tap into SAM-BA
is proven on the Titano. Builds/large compiles go on the controller, not the Pi.
