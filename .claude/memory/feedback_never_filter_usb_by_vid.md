---
name: feedback-never-filter-usb-by-vid
description: Never grep/filter lsusb or USB enumeration output by VID/PID
metadata:
  type: feedback
---

Never filter `lsusb` (or any USB enumeration check) by VID/PID — no `lsusb | grep -iE "239a|303a|..."`. Always print the full, unfiltered `lsusb` / `lsusb -t` and inspect dmesg without a VID filter.

**Why:** A DUT routinely changes VID/PID across modes — CircuitPython app (e.g. 239a:xxxx) → ROM/bootloader download mode (303a:1001 for ESP32-S3) → CP210x bridge (10c4:ea60) → UF2 bootloader. If you grep for the VID you *expect*, the moment the device enumerates with any other VID your filter hides it and you report "nothing here" — a false negative that sends you power-cycling/touching a device that was actually present, wasting the user's time and tokens and corrupting your mental model of the bus.

**How to apply:** For presence / stability checks prefer watching the
**`/dev/serial` tree in a loop** — e.g. `for i in 1 2 3 4 5; do ls -l
/dev/serial/by-id/ /dev/serial/by-path/; sleep 4; done` (or `tree /dev/serial`)
— NOT `lsusb | grep VID:PID`. The by-id name even tells you the *mode*
(`Adafruit_QT_Py_ESP32-S3…` app vs `Espressif_USB_JTAG_serial_debug_unit…` ROM),
and a port stable across iterations means the device isn't churning (self-
rebooting / being touched). The user explicitly asked for this (2026-06-13)
after I used `lsusb | grep -cE "303a:1001|239a:8143"`, which both filters by
VID/PID *and* gives a racy 0/1 that hid the self-reboot churn. When you do need
the raw bus, dump full `lsusb` + `lsusb -t` (a few lines); filter dmesg only by
*driver/interface* keywords (`ttyACM`, `ttyUSB`, `cp210x`, `error -`,
`disconnect`), never by idVendor/idProduct. See [[reference-hil-bench-usb-topology]].
