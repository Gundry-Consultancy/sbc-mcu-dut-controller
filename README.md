# Setup notes and Readme / Issues / Todos



## Issues / TODOs


### Issue: Cascading a USB 3.0 hub downstream of a Raspberry Pi USB 2.0 (pi zero and zero 2w and 3b) host causes kernel log spam (dwc_otg: Unknown hub control request) when devices are hot-plugged.

Cause: The USB 3.0 hardware attempts to negotiate Link Power Management (LPM) via a BOS descriptor request (wValue: f00h), which the Pi's standard USB 2.0 driver cannot handle.

Fix: Append dwc_otg.lpm_enable=0 to /boot/firmware/cmdline.txt and reboot to disable LPM negotiation and silence the error.

### Example API use:

Job that just loads WS to qtpy and runs protomq with logging for 5mins
```
SSH=/c/Windows/System32/OpenSSH/ssh.exe
PORT="/dev/serial/by-path/platform-3f980000.usb-usb-0:1.2:1.0"
BIN="/home/particle/dev-projects/python/usbip-hil-controller/run/jobs/firmware/9388d42b-5658-4360-a8c0-5295be65b86d/qtpy-combined.bin"
echo "=== bootloader prep ==="
for i in 1 2 3; do R=$("$SSH" particle@192.168.1.169 "ssh -i /etc/hil/keys/rpi-displays -o StrictHostKeyChecking=no pi@192.168.1.234 \"esptool.py --chip esp32s3 --port $PORT --before default_reset --after no_reset flash_id 2>&1 | tail -1\"" 2>&1 | tail -1); echo "  $i: $R"; echo "$R" | grep -q "bootloader" && break; sleep 2; done
read -r -d '' BODY <<JSON
{"target":{"device":{"id":"mcu-qtpy-oled-091-stemma"},"pool":"public"},"script":"firmware-bench",
"payload":{"kind":"firmware-bin","firmware":{"path":"$BIN","offset":"0x0"}},
"params":{"firmware":{"path":"$BIN","offset":"0x0"},
"stages":[{"type":"erase","after":"no_reset"},{"type":"flash","offset":"0x0","after":"no_reset"},{"type":"verify","offset":"0x0"},{"type":"power_cycle","off_s":1.0,"settle_s":3.0},{"type":"write_secrets_msc"},{"type":"power_cycle","off_s":1.0}],
"window_minutes":8,"esptool_chip":"esp32s3"},
"secrets":{"IO_USERNAME":"hil","IO_KEY":"hil","WIFI_SSID":"bench-wifi","WIFI_PASSWORD":"changeme"},
"secrets_profile":"bench-protomq","timeouts":{"total_s":7200,"deploy_s":1800,"run_s":3600,"flash_s":600}}
JSON
echo "=== submit full-loop ==="
"$SSH" particle@192.168.1.169 "curl -s -X POST http://127.0.0.1:8080/v1/jobs -H 'Authorization: Bearer dev-token-change-me' -H 'Content-Type: application/json' -d '$BODY'" 2>&1 | tail -1
```

Issue: Flashing an ESP32-S3/-C3 (or any board whose serial port is a native USB-Serial/JTAG bridge) intermittently fails — esptool stalls at "Connecting..." then "A fatal error occurred: Failed to connect to ESP32-S3: No serial data received.", and/or the board appears to reboot every couple of seconds (USB connect/disconnect churn in `dmesg`).

Cause: ModemManager (running by default on Raspberry Pi OS / Debian) auto-probes every new /dev/ttyACM*/ttyUSB* with AT commands and toggles the DTR/RTS control lines. On a native USB-Serial/JTAG bridge those lines are wired to the chip's EN/IO0 pins, so MM's probe can reset the chip, corrupt an in-progress flash, or simply hold the port during esptool's connect window. (Note: a separate, host-side failure mode looks similar — the Pi's legacy `dwc_otg` USB controller wedging with `WARN::dwc_otg_hcd_urb_dequeue: Timed out waiting for FSM NP transfer to complete`; that one needs a reboot, and the controller does NOT survive a driver unbind/rebind — it Oopses in `dwc_otg_driver_remove`.)

Fix: Disable and mask ModemManager — `sudo systemctl disable --now ModemManager && sudo systemctl mask ModemManager`. A plain `disable` is not enough: MM is D-Bus/udev activated and re-spawns the next time a tty appears, so it must be `mask`ed. `scripts/setup-hil-host.sh` now does this automatically.


Issue: The whole USB bus on a Raspberry Pi periodically wedges — `lsusb`, serial ports, and flashing all stop responding, with `dmesg` spamming `WARN::dwc_otg_hcd_urb_dequeue:639: Timed out waiting for FSM NP transfer to complete`. Devices linger as stale /dev nodes that never disconnect. A flaky DUT or a board stuck in a fast reset loop tends to trigger it.

Cause: `dwc_otg` — the legacy out-of-tree Broadcom USB driver used on all BCM283x boards (Pi 1/2/3/Zero/Zero 2 W) — recovers badly from USB error storms and can hang its host-controller state machine. It also cannot be reset at runtime: unbinding/rebinding the controller (`3f980000.usb`) Oopses with a NULL-pointer dereference in `dwc_otg_driver_remove`, so the only recovery is a full reboot.

Fix (optional): Switch to the mainline `dwc2` driver in host mode — add `dtoverlay=dwc2,dr_mode=host` to /boot/firmware/config.txt (or /boot/config.txt on older images) and reboot. `dwc2` is more robust under error storms and DOES support clean `unbind`/`bind`, enabling a targeted USB reset without rebooting. Trade-offs: (1) on boards whose onboard Ethernet hangs off USB (Pi 3B/3B+ via the LAN9514) host-mode `dwc2` can disrupt networking; (2) `dwc2` host-mode throughput is slightly lower, which is irrelevant for serial/JTAG flashing. Recommended on Pi Zero / Zero 2 W — those are Wi-Fi-only with no USB-attached Ethernet, so the trade-off is risk-free. `scripts/setup-hil-host.sh` auto-enables it on Pi Zero models and is opt-in elsewhere via `HIL_USE_DWC2=1`; set `HIL_USE_DWC2=0` to force-skip on a Zero.
