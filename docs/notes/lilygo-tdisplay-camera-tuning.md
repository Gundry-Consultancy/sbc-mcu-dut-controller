### 🔆 Brighter camera proof (re-run)

The earlier proof came out very dark. Root cause: the `capture_display` capture
was **6000 µs @ gain 1.0** — far too dim for the self-lit i8080 TFT on an
otherwise dark bench — and the ROI was loose, so the lit panel was a small dim
patch in a mostly-black crop. White-balance was uncontrolled (imx708 green cast).

Tuned on real hardware (csi-rpi-hil006 imx708) by sweeping exposure / autofocus /
white-balance against the live panel:

- **Exposure 32000 µs, gain 3.0** (was 6000 µs / 1.0)
- **Autofocus converged at 1.40 dioptre, then locked to manual** — continuous AF
  drifted during the still grab and blurred the text
- **White-patch white balance** on the crop (off the lit text) — neutralises the
  green cast; the camera-server itself ignores WB query params
- **Tighter ROI** `1270,770,235,135 @2304` (scales to the 4608 full-res frame)

Re-run end-to-end through the HIL pipeline (flash → v2 check-in → inject display
Add + Write → capture):

```
CHECKIN_VERDICT ok=true uid=lilygo-t-display-s321621634 proto=v2
INJECT_VERDICT published=true kind=display_add_i8080 resp={'status': 'OK'}
INJECT_VERDICT published=true kind=raw (display Write) resp={'status': 'OK'}
DISPLAY_CAPTURE_VERDICT saved=... exposure_us=32000 gain=3.0 focus=1.400 wb=yes frame=4608x2592 cropped=yes
```

![LilyGo T-Display-S3 i8080 ST7789 — bright HIL camera proof](https://github.com/tyeth-ai-assisted/adafruit-Adafruit_Wippersnapper_Arduino/releases/download/hil-proof/lilygo-tdisplay-s3-bright.jpg)

The panel clearly shows the status bar + `LilyGo T-Display-S3 / WipperSnapper v2 /
i8080 ST7789 / HIL camera proof OK`. Capture defaults updated in
`.github/scripts/hil_lilygo_display.py` and the controller `capture_display` stage.
