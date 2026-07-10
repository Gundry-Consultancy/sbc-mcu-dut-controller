# QT Py ESP32-S3 N4R2 — mux'd I2C sensor HIL testing

How to test the I2C sensor drivers from **Adafruit_Wippersnapper_Arduino PR #933**
(22 backported v2 drivers) and **Wippersnapper_Components PR #337** (their settings)
on a real **string of I2C sensors behind a TCA9548A multiplexer**, attached to the
**QT Py ESP32-S3 N4R2** on bench host **rpi-hil006**.

This doc is self-contained: a fresh session with no prior context can act on it.
Hard-won mechanics (especially flashing a native-USB-JTAG ESP32-S3 while **holding a
mux channel latched**) are written down so they don't have to be rediscovered.

> Companion sources: the sensor plan is `~/Downloads/hil_i2c_components.ods` (4 sheets:
> Component_Matrix, HIL_Mux_Layout, Address_Conflicts, Test_Fixtures). PRs:
> [WS-Arduino #933](https://github.com/adafruit/Adafruit_Wippersnapper_Arduino/pull/933),
> [WS-Components #337](https://github.com/adafruit/Wippersnapper_Components/pull/337).

---

## 1. Access / environment

| Thing | Value |
|---|---|
| Bench host | **rpi-hil006** (Pi 4, xhci USB) |
| SSH to host | **git-bash OpenSSH** `/usr/bin/ssh -i /tmp/hilkey pi@rpi-hil006` — copy key `~/Downloads/rpi-hil-fleet` → `/tmp/hilkey`, `chmod 600`. **Not** Windows ssh for the fleet. |
| Controller (tachyon) | `http://192.168.1.169:8080`, bearer `dev-token-change-me` |
| SSH to tachyon | **Windows** OpenSSH `/c/Windows/System32/OpenSSH/ssh.exe particle@192.168.1.169` (its ssh-agent holds the key; the fleet key does NOT auth here) |
| Device id | `mcu-qtpy-esp32s3-n4r2-hil006` |
| Serial (stable) | by-path `/dev/serial/by-path/platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.2.1.1:1.0` → `ttyACM0` |
| Power recovery | physically **solenoid ch0**: `~/turn_off.sh 0 8 && ~/turn_on.sh 0` (long OFF hold clears a wedged native-USB board). A **power cut resets the mux.** |
| Host has internet | yes — `curl`/download directly ON the host |

**Transfer rule: never base64 binaries over SSH (insanely slow).** Use `scp -i /tmp/hilkey file pi@rpi-hil006:/tmp/` or download on the host with `curl`. (HTTP POST to the controller `/v1/firmware` is also fine.)

---

## 2. The sensor string (discovered via CircuitPython I2C scan)

One **TCA9548A @ 0x77** (8-ch). All on the QT Py **STEMMA QT** bus = MicroPython/CP
`SoftI2C(scl=Pin(40), sda=Pin(41))` / CircuitPython `board.STEMMA_I2C()` (bus 1).
(The pad bus `board.I2C()` = GPIO6/7 has no pull-ups — nothing there.)

| Location | Addr | Part | PR #933 driver | PR #337 settings |
|---|---|---|---|---|
| direct | `0x58` | **SGP30** | ✅ `drvSgp30` | — |
| direct | `0x12` | PMSA003I (high-current, currently **removed**) | existing | — |
| ch0 | `0x52` | APDS9999 | in v2 | — |
| ch0 | `0x59` | **SGP40** (shares 0x59 w/ SGP41) | ✅ `drvSgp40` | — |
| ch0 | `0x74` | **AS7331** (UV) | ✅ `drvAs7331` | ✅ as7331 |
| ch0 | `0x76` | **BME280** | existing | ✅ sea_level_pressure |
| ch0 | `0x61` | SCD30 (high-current, currently **removed**) | existing | — |
| ch1 | `0x48` | **TMP119** | ✅ `drvTmp119` | — |
| ch1 | `0x53` | ENS160 | existing | — |

IDs confirmed by register reads (TMP119 `0x0F`=`0x2117`; AS7331 AGEN `0x21`; BME280 `0xD0`=`0x60`; SGP40 (0x59, shares addr w/ SGP41) featureset `0x202F`=`0x3240`; SGP30 serial-cmd ACK; ENS160 PART_ID `0x0160`).
**Power note:** the high-current sensors (SCD30 CO2, PMSA003I fan) were physically removed to reduce inrush during re-enumeration.

---

## 3. USB identity = firmware mode (read it off lsusb)

| VID:PID | Product string | Mode |
|---|---|---|
| `239a:8143` | "QT Py ESP32-S3 (4MB Flash 2MB PS…)" | **WipperSnapper** app |
| `239a:8144` | "QT Py ESP32S3 4MB Flash 2MB PSRAM" | **CircuitPython** app |
| `239a:0143` | "QT Py ESP32-S3 (4M Flash, 2M PSRAM)" | **tinyuf2 bootloader** (exposes `QTPYS3BOOT` UF2 MSC drive at `/dev/sda`) |
| `303a:1001` | "Espressif USB JTAG/serial debug unit" | **ROM USB-Serial/JTAG download** (esptool-compatible) |
| `303a:4001` | "Espressif Systems Espressif Device" | MicroPython app |

UF2 family id for ESP32-S3 = `c47e5767` (first block offset 28).

---

## 4. The flashing reality (the core learnings)

This native-USB-JTAG ESP32-S3 has **no UART bridge** — esptool can only reach the ROM
download loader (`303a:1001`) if the **currently-running firmware flips the USB into it**
on a **1200-baud touch**. Whether that works depends on the app:

| Running firmware | 1200-touch (`stty -F <port> 1200`) → `303a:1001`? | esptool `--before default-reset`? |
|---|---|---|
| **WipperSnapper / Arduino** | ✅ yes (≈1s) | ✅ yes |
| **tinyuf2 bootloader** | ✅ yes | ✅ yes |
| **CircuitPython** | ❌ **no-op** (its TinyUSB CDC ignores both) | ❌ ignored |
| **MicroPython** | ❌ (TinyUSB `303a:4001`, ignores) | ❌ ignored |

So **CircuitPython cannot be flashed *from* via esptool** (no port ever opens). Proven
dead-ends from CP: `RunMode.UF2` reboots back to CP if tinyuf2 is absent;
`RunMode.BOOTLOADER` enters a USB-DFU/download state that **does not enumerate on this
Pi** (`device descriptor read error -110`, no port — not catchable by esptool, not fixed
by removing sensors or rebooting the Pi); raw RTC `FORCE_DOWNLOAD_BOOT` via `machine.mem32`
sets but a software reset doesn't honor it. **We do not use dfu-util.**

### Entering ROM download without a power cut (mux-preserving)

The point of a *soft* download entry is that it doesn't power-cycle, so a
TCA9548A channel latched from the REPL survives into the flash. What actually
works, per running firmware:

| method | works from | ? | notes |
|---|---|---|---|
| esptool `--before default-reset` (USB-JTAG reset) | WS/Arduino, tinyuf2, **MicroPython** | ✅ | JTAG reset is soft — latch held |
| 1200-baud touch → ROM | WS/Arduino, tinyuf2 | ✅ | the table above; CP/MP ignore it |
| `machine.bootloader()` | **MicroPython** | ✅ | MP's clean soft download entry |
| `microcontroller.on_next_reset(RunMode.BOOTLOADER)` + `reset()` | CircuitPython | ⚠️ | the `reset()` must fire **inside** the raw-REPL block, and a watchdog `RESET` (~2 s) is the most reliable trigger — but on this Pi the resulting DFU state **doesn't enumerate** (`-110`), a dead end here |
| RTC `FORCE_DOWNLOAD_BOOT` bit via `machine.mem32` | MP / CP | ⚠️ | sets the bit but a plain software reset doesn't honor it; the register offset is unclear (seen as both `0x60008128` and `0x6000812C`) — verify against the ESP32-S3 TRM before relying on it |

A solenoid power-cycle is the guaranteed way to **clear** a stuck RTC/BOOTLOADER
flag back to the app — but it also resets the mux latch.

> **Scope note:** this board carries its **own** on-DUT TCA9548A for *its* STEMMA
> sensors. For sensors **shared between DUTs**, the platform now routes a whole
> strand to one DUT at a time via the controller's **ADG729 analog strand-mux**
> (break-before-make) — see the `hil-i2c-strands` skill — not an on-DUT mux latch.
> The REPL latch tricks here are for a single board's local bus.

### The fix: tinyuf2 in the chain

Flash the **tinyuf2 bootloader** first; then CircuitPython has somewhere to hand off to
(`RunMode.UF2` → tinyuf2), and firmware swaps become **UF2 drag-drop** or **esptool via the
stty→ROM route** — all **soft resets + USB-MSC writes, no power cut, so the mux channel is
held.** (A mux latch on the TCA9548A only resets on an actual power cut.)

Get the tinyuf2 image from GitHub releases (do NOT base64 over ssh):
```bash
# on rpi-hil006
BOARD=adafruit_qtpy_esp32s3_n4r2
TAG=$(curl -s https://api.github.com/repos/adafruit/tinyuf2/releases/latest | grep -oE '"tag_name": *"[^"]+"' | head -1 | sed -E 's/.*"([^"]+)".*/\1/')   # 0.35.0
curl -sL -o /tmp/tinyuf2.zip "https://github.com/adafruit/tinyuf2/releases/download/$TAG/tinyuf2-$BOARD-$TAG.zip"
cd /tmp && unzip -o tinyuf2.zip   # -> combined.bin (flash at 0x0), bootloader.bin, partition-table.bin, *.uf2
```

---

## 5. The mux-preserving workflow (proven)

Two interchangeable routes once tinyuf2 is installed. **All steps are soft → the TCA9548A
channel set in step 1 survives to the running WS app.** Verified: ch0 (`0x52/0x59/0x74/0x76`)
still present after a CP→tinyuf2→CP round-trip with **no re-latch**.

### One-time bring-up (board currently on anything)
1. Flash **tinyuf2 `combined.bin` @ 0x0** via esptool. From CP this needs the controller's
   power-cycle recovery (see §6) or the §7 ROM-window catch. From WS, use the stty→ROM route (§5b).
2. Board boots tinyuf2 → `QTPYS3BOOT` drive. Install CircuitPython:
   ```bash
   curl -sL -o /tmp/cp.uf2 'https://downloads.circuitpython.org/bin/adafruit_qtpy_esp32s3_4mbflash_2mbpsram/en_US/adafruit-circuitpython-adafruit_qtpy_esp32s3_4mbflash_2mbpsram-en_US-10.2.1.uf2'
   DEV=$(lsblk -o NAME,LABEL -nr | awk '/QTPYS3BOOT/{print "/dev/"$1; exit}')
   sudo mount "$DEV" /mnt/uf2 && sudo cp /tmp/cp.uf2 /mnt/uf2/ && sync && sudo umount /mnt/uf2  # tinyuf2 flashes + boots CP
   ```

### 5a. Per channel: CircuitPython → WipperSnapper (UF2 route)
1. **CP REPL** (raw REPL via pyserial; CP `board.STEMMA_I2C()`):
   ```python
   import board, time, microcontroller
   i2c = board.STEMMA_I2C(); i2c.try_lock(); i2c.writeto(0x77, bytes([1 << CH])); i2c.unlock()  # latch channel CH
   microcontroller.on_next_reset(microcontroller.RunMode.UF2); microcontroller.reset()           # -> tinyuf2, soft
   ```
2. Wait for `QTPYS3BOOT` (`239a:0143`), then copy `ws.uf2` onto it (mount + cp + sync). WS boots, mux still on CH.

### 5b. WipperSnapper → CircuitPython, or any esptool op (stty→ROM route)
From a **WS or tinyuf2** state (NOT CP):
```bash
PORT=/dev/serial/by-path/platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.2.1.1:1.0
stty -F "$PORT" 1200                                  # touch -> 303a:1001 in ~1s
esptool --chip esp32s3 --port "$PORT" --before no-reset --after watchdog-reset flash-id   # read/erase/write; watchdog-reset boots app back
```
`--before no-reset` (already in ROM) + `--after watchdog-reset` (soft boot, mux held).
**Caveat:** esptool-flashing a *combined* `.bin` at 0x0 **overwrites tinyuf2**. To keep
tinyuf2, install CP/WS via the **UF2 drive**, or esptool only the app partition. (Open
item: a clean WS→CP that preserves tinyuf2 — WS's touch goes to `303a:1001`, not tinyuf2,
so the UF2 drag-drop route isn't directly reachable from WS; see §8.)

---

## 6. Controller (firmware-bench) — the reliable flasher

The controller's `firmware-bench` is the proven way to flash + recover, used by PR #930
check-in and the `hil-bisect` engine. It enters download via the **1200-touch** (works on
WS), and recovers a wedged/boot-looping board via **power-cycle + `force_download_via_reset`**
(esptool `--before default-reset --after no-reset flash-id` tight loop catching the ROM up-window).

**Fix deployed 2026-06-22 (commit `f425ed0`, Gundry-Consultancy fork):** `_recover_download_via_hub`
power-cycled then **slept `boot_settle_s` (default 5.0s)** before the catch loop — so on a
native-USB board (CP boots in ~1.6s) the ROM window was already closed. Fixed: power-cycle
with `settle_s=0.0`, run `force_download_via_reset` immediately, default `boot_settle_s`
**5.0 → 0.01**. Also set `solenoid_channel: 0` on the device so recovery can power-cycle.

> **But `solenoid_channel: 0` makes `power_cycle` a real power cut → resets the mux.** For a
> mux-preserving controller flow, the `power_cycle` stages must use a **soft** reset
> (`--after watchdog-reset`) instead of the solenoid. Reconsider the topology `solenoid_channel`
> or add a soft-reset stage option. (See §8.)

Flash via controller (recovers CP→download, writes any combined.bin):
```bash
FW=$(curl -s -X POST 'http://192.168.1.169:8080/v1/firmware?filename=x.bin' \
      -H 'Authorization: Bearer dev-token-change-me' --data-binary @/tmp/combined.bin | jq -r .path)
# POST /v1/jobs script=firmware-bench, stages: enter_bootloader, erase(no_reset), flash(0x0,no_reset),
#   power_cycle [, write_secrets_msc, power_cycle, verify_checkin]; poll /v1/jobs/{id}/wait?since=&timeout=
```
A full flash→secrets→checkin job **succeeded** with the PR #933 build: WS `2.0.0-alpha.1`
flashed, WiFi+broker, broker `R_OK` + `checkin complete`. (The `verify_checkin` *stage* is
v1-oriented and may report job `error` even though the v2 device checked in — read the
broker/serial logs, not just the verdict.)

### Building the WS combined.bin
CI `build-files` artifact has only the **app-only** `.bin` (not flashable at 0x0) + a `.uf2`.
For esptool you need the **combined** image = tinyuf2 `bootloader.bin`@0x0 +
`partition-table.bin`@0x8000 + arduino-esp32 `boot_app0.bin`@0xe000 + app@0x10000
(`esptool merge-bin --flash-mode dio --flash-freq 80m --flash-size 4MB`). For the UF2 route,
just use the CI `.uf2` directly (family `c47e5767`).

---

## 7. Recovery cheatsheet

- **Board off-bus / `error -110` / `not accepting address` (boot-loop):** solenoid power-cycle
  `~/turn_off.sh 0 8 && ~/turn_on.sh 0` (mux resets). If the **Pi's** xhci is wedged after an
  error storm (5+ day uptime), `sudo reboot` rpi-hil006 — the QT Py re-enumerates clean on boot.
- **Normal POR** boots the app (tinyuf2 → boots CP/WS if a valid app exists).
- Resolve the port fresh each time (`/dev/serial/by-path/...1.2.1.1:1.0`); after a reset it
  may briefly vanish — **be patient** (poll up to ~15-30s) before declaring a failure.

---

## 8. Open items (to finish the actual sensor test)

1. **Get WS to read the muxed sensors.** After UF2-swapping to WS (mux held), WS halts with
   "settings.json … default values". Write `settings.json` (IO/WiFi/broker) to the **WIPPER**
   MSC drive (host mount, no power cut) + **soft reset**, then WS needs a **broker** to check
   in and a **v2 I2C device-add** to add the ch-N components and read them. Only a v1
   `PixelsWrite` injector exists (`adapters/ws_signal_inject.py`); a **v2 `I2CDeviceAddOrReplace`
   encoder** over the protomq broker (`vendor/protomq` `POST /api/echo {topic,payload}`) is the
   missing piece. Alternative: point `settings.json` at real `io.adafruit.com` + add components in the IO UI.
2. **Mux-preserving controller flow.** Make `firmware-bench` `power_cycle` use `--after
   watchdog-reset` (soft) instead of the solenoid, so a full flash→secrets→checkin holds the
   mux. Then per-channel testing is: CP set mux → tinyuf2 → controller flashes WS + secrets +
   checkin (all soft) → read channel.
3. **Clean WS→CP keeping tinyuf2.** WS's touch → `303a:1001` (not tinyuf2), so the UF2 route
   isn't directly reachable from WS; esptool-flashing CP `combined.bin` overwrites tinyuf2.
   Options: esptool only the CP **app** partition (keep tinyuf2 bootloader), or re-flash tinyuf2
   each cycle, or verify a double-tap/alternate path WS→tinyuf2.

---

## 9. Helper scripts

Two of the reusable ones are committed under [`scripts/`](../scripts):

- [`scripts/uf2_to_bin.py`](../scripts/uf2_to_bin.py) — convert a WS-v2 CI `.uf2`
  into a flat `.bin` esptool can flash at `0x0` (CI ships only a `.uf2`).
- [`scripts/i2c_id_probe.py`](../scripts/i2c_id_probe.py) — read sensor ID/PART_ID
  registers over the CircuitPython REPL to disambiguate ambiguous addresses; the
  `JOBS` table doubles as an id-register reference.

The rest were single-use bring-up throwaways (touch→esptool flash loops, CP↔WS
UF2 round-trip proofs, RTC-download experiments); their durable knowledge is the
commands in §4–§6 and the soft-download matrix above, not the scripts themselves.
