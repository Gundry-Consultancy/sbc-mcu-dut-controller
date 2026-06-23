---
name: hil-mux-i2c-swap
description: "Flash/swap firmware on the native-USB-JTAG QT Py ESP32-S3 N4R2 (device mcu-qtpy-esp32s3-n4r2-hil006 on rpi-hil006) while HOLDING a TCA9548A I2C mux channel latched, to HIL-test mux'd I2C sensors (e.g. Wippersnapper-Arduino PR #933 drivers + Components PR #337 settings). Use when: testing I2C sensors behind a mux on this board, swapping CircuitPython<->WipperSnapper without losing the mux channel, or recovering/flashing this board when CircuitPython won't enter download mode. Covers the tinyuf2 + UF2-swap + stty->esptool mechanics and why CircuitPython can't be esptool-flashed directly. NOT for: a normal already-WS board (submit firmware-bench directly), or boards with a real UART bridge."
---

# hil-mux-i2c-swap

Test I2C sensors behind a **TCA9548A mux** on the **QT Py ESP32-S3 N4R2**
(`mcu-qtpy-esp32s3-n4r2-hil006`, host **rpi-hil006**) by swapping firmware while the
**mux channel stays latched**. Full detail + access creds + sensor inventory:
[`docs/qtpy-mux-i2c-hil.md`](../../docs/qtpy-mux-i2c-hil.md) — read it first.

## The core problem
This board is **native USB-JTAG (no UART bridge)**. esptool can only reach the ROM
download loader (`303a:1001`) if the running firmware **flips into it on a 1200-baud touch**.
**WipperSnapper/Arduino and tinyuf2 honor the touch; CircuitPython does NOT** (its TinyUSB
CDC ignores the touch *and* esptool's reset). A TCA9548A channel only resets on a **power
cut** — so any flash that power-cycles loses the mux latch.

## The solution (proven): tinyuf2 + soft swaps
Flash the **tinyuf2 bootloader** once; then all swaps are soft (no power cut) → **mux held**.

1. **CP sets the mux channel**, then hands to tinyuf2:
   ```python
   import board, time, microcontroller
   i2c=board.STEMMA_I2C(); i2c.try_lock(); i2c.writeto(0x77, bytes([1<<CH])); i2c.unlock()
   microcontroller.on_next_reset(microcontroller.RunMode.UF2); microcontroller.reset()  # -> tinyuf2 QTPYS3BOOT
   ```
2. **Swap firmware**, mux-preserving, either route:
   - **UF2 drag-drop** (CP→WS): mount `QTPYS3BOOT` (`/dev/sda`), `cp ws.uf2` to it, `sync` → WS boots.
   - **stty→esptool** (from WS or tinyuf2, NOT CP): `stty -F <port> 1200` → `303a:1001`, then
     `esptool --chip esp32s3 --port <port> --before no-reset --after watchdog-reset <flash-id|erase-flash|write-flash …>`.
3. WS comes up with the channel still selected → its I2C scan/components see the direct bus + that channel.

`RunMode.UF2` works **only because tinyuf2 is present** (flashing CP's combined `.bin` at 0x0
overwrites tinyuf2 — install CP/WS via the **UF2 drive**, not esptool combined.bin, to keep it).

## Reliable flasher = the controller
`firmware-bench` (this repo) flashes via the 1200-touch and recovers wedged boards via
power-cycle + `force_download_via_reset` (fix in `f425ed0`: no `boot_settle` before the catch).
It's the way to flash from a CP/wedged state — but its `power_cycle` uses the **solenoid (power
cut → mux reset)** when `solenoid_channel` is set; for mux-preserving flows it must use
`--after watchdog-reset` instead. A full PR #933 flash→secrets→checkin via the controller is
proven (WS `2.0.0-alpha.1`, broker `R_OK`).

## Gotchas
- USB VID:PID = mode: `239a:8143`=WS, `239a:8144`=CircuitPython, `239a:0143`=tinyuf2
  (`QTPYS3BOOT` drive), `303a:1001`=ROM download (esptool), `303a:4001`=MicroPython.
- **Never base64 binaries over SSH** — use `scp` / host `curl`. Fleet SSH = git-bash
  `/usr/bin/ssh -i /tmp/hilkey pi@rpi-hil006`; tachyon = Windows OpenSSH `particle@192.168.1.169`.
- Be **patient after a reset** (poll the by-path port ~15-30s; it briefly vanishes).
- Recover a wedged board: `~/turn_off.sh 0 8 && ~/turn_on.sh 0` (mux resets); if the **Pi's**
  xhci is wedged after an error storm, `sudo reboot` rpi-hil006.
- High-current sensors (SCD30, PMSA003I) can brown out re-enumeration — remove if flaky.

## Open / next
To actually read the muxed sensors, WS still needs a **broker + secrets + a v2 I2C
device-add** (only a v1 PixelsWrite injector exists). See `docs/qtpy-mux-i2c-hil.md` §8.
