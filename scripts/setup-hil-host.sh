#!/usr/bin/env bash
# Provision a HIL bench host (idempotent — safe to re-run).
#
# Usage (run as root on the target host):
#   bash setup-hil-host.sh <user> <pubkey-file>
#
# Example:
#   sudo bash setup-hil-host.sh pi ~/.ssh/id_ed25519.pub
#
# What it ALWAYS does (base provisioning):
#   1. Adds <user> to gpio, i2c, plugdev (SPI), dialout (UART), video groups.
#   2. udev rules: SPI access; USB autosuspend OFF on bench hubs + native-USB MCUs
#      (stops the DUT's serial/JTAG/MSC endpoints suspending mid-job); dialout perms
#      on serial-adapter VIDs; RP2040/picotool perms.
#   3. polkit rule: lets <user> (plugdev) mount a DUT's USB-MSC volume via udisksctl
#      headlessly (no seat session) — write_secrets_msc, no NotAuthorized fallback.
#   4. Masks ModemManager (it probes/RESETs serial ports mid-flash).
#   5. Pi-only: cmdline.txt `fsck.mode=force fsck.repair=yes` so a dirty rootfs
#      auto-repairs on boot instead of dropping to read-only maintenance (WARNED, 3s).
#   6. Passwordless sudo for <user> (REQUIRED — non-TTY SSH needs root for mount etc).
#   7. Flashing toolchain: esptool (pip), bossac (SAM/SAMD51), udisks2, usbip,
#      socat, i2c-tools, git, gh.
#   8. usbip: sudoers + kernel modules + usbipd service (USB-server hosts).
#   9. Installs the public key into ~<user>/.ssh/authorized_keys.
#
# OPTIONAL capabilities (opt in via env var):
#   HIL_SOLENOID_HUB=1        Install Blinka + MCP23017 libs and deploy the solenoid
#                             CLI to /opt/hil (controls a USB hub's power buttons),
#                             plus ~/turn_on.sh ~/turn_off.sh ~/all_on.sh ~/all_off.sh.
#     HIL_SOLENOID_I2C_ADDRESS=0x20   MCP23017 address (A0/A1/A2 jumpers; default 0x20).
#     HIL_SOLENOID_CHANNELS="0 1 2 3 4 5 6 7"   channels ~/all_on.sh pulses.
#   HIL_CAMERA_SERVER=1       Install picamera2/opencv + deploy the snapshot server
#                             (tools/camera-server) as hil-camera.service on :8080.
#     HIL_CAMERA_OVERLAY=imx519   dtoverlay for a sensor the firmware can't auto-detect
#                             (added under [all]); imx519 also gets the AF tuning patch.
#     HIL_CAMERA_NEOPIXEL=1   Drive a NeoPixel illuminator ring (unit runs as root from
#                             /opt/hil/venv; onboard audio is disabled for PWM pins).
#       HIL_NEOPIXEL_PIN=D18          data pin (rpi_ws281x: D10 D12 D13 D18 D19 D21).
#       HIL_NEOPIXEL_COUNT=32         pixels in the ring.
#       HIL_NEOPIXEL_MAX_BRIGHTNESS=255   server-side ceiling (0..255; 102 = 40%).
#   HIL_USE_DWC2=0|1          Force the mainline dwc2 USB driver (default: on for Pi Zero).
#   HIL_USBIP_CLIENT=0|1      Load vhci-hcd (usbip client) at boot (default: off on a
#                             Raspberry Pi DUT host, where it renumbers the bench USB bus).
#   HIL_WORK_ON_DISK=0|1      Bind /tmp/hil onto ~/hil-work (default: on below 1 GiB RAM).
#
# Group → device mapping (Linux):
#   gpio    → /dev/gpiochip*   (GPIO, bit-bang 1-wire)
#   i2c     → /dev/i2c-*       (MCP23017 solenoid hub, I2C peripherals)
#   plugdev → /dev/spidev*, udisks mount (polkit rule above)
#   dialout → /dev/ttyS*, /dev/ttyUSB*, /dev/ttyACM*  (UART, USB-serial)
#   video   → /dev/video*      (camera capture)
#
# Run `newgrp <group>` or log out/in after this script for group changes to take effect.

set -euo pipefail

HIL_USER="${1:-}"
PUBKEY_FILE="${2:-}"

if [[ -z "$HIL_USER" || -z "$PUBKEY_FILE" ]]; then
    echo "Usage: $0 <user> <pubkey-file>" >&2
    exit 1
fi

if [[ "$(id -u)" -ne 0 ]]; then
    echo "Must be run as root." >&2
    exit 1
fi

if ! id "$HIL_USER" &>/dev/null; then
    echo "User '$HIL_USER' does not exist." >&2
    exit 1
fi

if [[ ! -f "$PUBKEY_FILE" ]]; then
    echo "Public key file '$PUBKEY_FILE' not found." >&2
    exit 1
fi

GROUPS_NEEDED=(gpio i2c plugdev dialout video)

for grp in "${GROUPS_NEEDED[@]}"; do
    if ! getent group "$grp" &>/dev/null; then
        echo "Creating group: $grp"
        groupadd "$grp"
    fi
    if id -nG "$HIL_USER" | grep -qw "$grp"; then
        echo "  $HIL_USER already in $grp"
    else
        echo "  Adding $HIL_USER to $grp"
        usermod -aG "$grp" "$HIL_USER"
    fi
done

# udev + polkit rules — non-root hardware access AND USB stability on a flashing
# bench. Write each rule only when its content differs (idempotent, quiet re-runs).
_install_rule() {  # path   (rule content on stdin)
    local path="$1" tmp; tmp="$(mktemp)"; cat > "$tmp"
    if [[ -f "$path" ]] && cmp -s "$tmp" "$path"; then
        echo "  rule already current: $path"; rm -f "$tmp"
    else
        install -m 0644 "$tmp" "$path"; rm -f "$tmp"
        echo "  rule written: $path"
    fi
}

# SPI: on Tachyon (and many Linux SBCs) spidev is root-only by default.
# Adafruit_Wippersnapper_Python README mandates this rule for Tachyon users.
_install_rule /etc/udev/rules.d/99-spi.rules <<'EOF'
SUBSYSTEM=="spidev", GROUP="plugdev", MODE="0660"
EOF

# USB autosuspend OFF on bench hubs + native-USB MCUs. Without this the DUT's
# serial / USB-JTAG / MSC endpoints autosuspend and vanish mid-job — surfacing as
# esptool "No serial data received", serial-capture "could not open port ... No
# such file", and flaky recovery after a firmware-induced crash/reboot. Covers
# Genesys (05e3) + VIA (2109) bench hubs and Adafruit (239a) / Espressif (303a)
# native USB. If your hub's idVendor differs, add it here (`lsusb` to find it).
_install_rule /etc/udev/rules.d/99-usb-no-autosuspend.rules <<'EOF'
ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="05e3", ATTR{power/control}="on", ATTR{power/autosuspend}="-1"
ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="2109", ATTR{power/control}="on", ATTR{power/autosuspend}="-1"
ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="239a", ATTR{power/control}="on", ATTR{power/autosuspend}="-1"
ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="303a", ATTR{power/control}="on", ATTR{power/autosuspend}="-1"
EOF

# Serial-adapter permissions: dialout/0664 on the common DUT serial VIDs so the
# bench user (in dialout) opens the port cleanly without a perms race. Espressif
# (303a) / Adafruit (239a) native USB, SiLabs CP210x (10c4), CH34x (1a86), FTDI (0403).
_install_rule /etc/udev/rules.d/99-usb-serial.rules <<'EOF'
SUBSYSTEM=="tty", ATTRS{idVendor}=="303a", GROUP="dialout", MODE="0664"
SUBSYSTEM=="tty", ATTRS{idVendor}=="239a", GROUP="dialout", MODE="0664"
SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", GROUP="dialout", MODE="0664"
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", GROUP="dialout", MODE="0664"
SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", GROUP="dialout", MODE="0664"
EOF

# Picotool / RP2040 (2e8a) device permissions — for Pico-class DUTs.
_install_rule /etc/udev/rules.d/98-Picotool.rules <<'EOF'
SUBSYSTEM=="usb", ATTRS{idVendor}=="2e8a", ATTRS{idProduct}=="0003", MODE="660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTRS{idVendor}=="2e8a", ATTRS{idProduct}=="000a", MODE="660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTRS{idVendor}=="2e8a", ATTRS{idProduct}=="f00a", MODE="660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTRS{idVendor}=="2e8a", ATTRS{idProduct}=="000f", MODE="660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTRS{idVendor}=="2e8a", ATTRS{idProduct}=="f00f", MODE="660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTRS{idVendor}=="2e8a", ATTRS{idProduct}=="0003", MODE="660", GROUP="dialout"
SUBSYSTEM=="usb", ATTRS{idVendor}=="2e8a", ATTRS{idProduct}=="000a", MODE="660", GROUP="dialout"
SUBSYSTEM=="usb", ATTRS{idVendor}=="2e8a", ATTRS{idProduct}=="f00a", MODE="660", GROUP="dialout"
SUBSYSTEM=="usb", ATTRS{idVendor}=="2e8a", ATTRS{idProduct}=="000f", MODE="660", GROUP="dialout"
SUBSYSTEM=="usb", ATTRS{idVendor}=="2e8a", ATTRS{idProduct}=="f00f", MODE="660", GROUP="dialout"
EOF

# polkit: let the bench user mount/unmount a DUT's USB-MSC volume via udisksctl
# headlessly (write_secrets_msc). Over SSH there is no active seat session, so udisks2
# escalates the mount to the *-other-seat action and the default policy demands
# auth_admin — `udisksctl mount` fails NotAuthorized (a misconfig smell, not benign;
# we'd fall back to `sudo mount`). Grant ALL filesystem-mount* variants (mount,
# -system, -other-seat: a seatless SSH caller needs other-seat) + unmount-others to
# plugdev so the rootless path works cleanly. polkit JS rules: Debian 12+/trixie.
_install_rule /etc/polkit-1/rules.d/50-hil-udisks.rules <<'EOF'
polkit.addRule(function(action, subject) {
    if (subject.isInGroup("plugdev") && (
            action.id.indexOf("org.freedesktop.udisks2.filesystem-mount") === 0 ||
            action.id == "org.freedesktop.udisks2.filesystem-unmount-others")) {
        return polkit.Result.YES;
    }
});
EOF

udevadm control --reload-rules
udevadm trigger                                      # change events: perms/groups
udevadm trigger --action=add --subsystem-match=usb   # re-apply power/control=on now (no reboot)
systemctl try-restart polkit 2>/dev/null || true
echo "  udev + polkit rules reloaded + triggered"

# ModemManager — disable on a flashing bench. MM auto-probes every new
# /dev/ttyACM*/ttyUSB* with AT commands and toggles DTR/RTS. On boards whose
# serial is a native USB-Serial/JTAG bridge (ESP32-S3/-C3, etc.) those control
# lines are wired to EN/IO0, so MM's probe can RESET the chip, clobber a flash
# in progress, or hold the port long enough that esptool's connect fails with
# "No serial data received". Mask it — a plain `disable` is not enough because
# MM is D-Bus/udev activated and will be re-spawned the next time a tty appears.
if systemctl list-unit-files 2>/dev/null | grep -q '^ModemManager\.service'; then
    systemctl disable --now ModemManager 2>/dev/null || true
    if systemctl mask ModemManager 2>/dev/null; then
        echo "  ModemManager masked (cannot grab serial / USB-JTAG ports)"
    else
        echo "  WARNING: could not mask ModemManager" >&2
    fi
else
    echo "  ModemManager not installed — nothing to disable"
fi

# USB host driver — optionally switch the legacy dwc_otg controller to the
# mainline dwc2 driver in host mode. dwc_otg (the out-of-tree Broadcom driver on
# BCM283x: Pi 1/2/3/Zero/Zero 2 W) recovers badly from USB error storms — it
# wedges with "WARN::dwc_otg_hcd_urb_dequeue: Timed out waiting for FSM NP
# transfer to complete" and cannot be reset at runtime (unbind/rebind Oopses in
# dwc_otg_driver_remove), so its only recovery is a reboot. dwc2 is the more
# robust mainline driver and supports clean unbind/rebind (targeted USB reset
# without rebooting).
#
# Decision: RECOMMENDED and enabled by default on Pi Zero / Zero 2 W — they are
# Wi-Fi-only with no USB-attached Ethernet, so the dwc2 trade-off is risk-free.
# Opt-in elsewhere (HIL_USE_DWC2=1), because on boards whose Ethernet hangs off
# USB (Pi 3B/3B+ LAN9514) host-mode dwc2 can disrupt networking. An explicit
# HIL_USE_DWC2=0/1 always wins. Takes effect on the next reboot.
MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)"
case "$MODEL" in *Zero*) IS_ZERO=1 ;; *) IS_ZERO=0 ;; esac
CONFIG_TXT=/boot/firmware/config.txt
[[ -f "$CONFIG_TXT" ]] || CONFIG_TXT=/boot/config.txt
# config.txt helpers. A line only applies to every board when it sits before the first
# conditional section or under [all]; stock Pi OS images already carry lines such as
# `dtoverlay=dwc2,dr_mode=host` under [cm5], which must NOT count as "already present".
_cfg_all_has() {  # <ERE> — true if an active line matching ERE applies to all boards
    [[ -f "$CONFIG_TXT" ]] || return 1
    awk -v re="$1" 'BEGIN { s = 1 }
        /^[[:space:]]*\[/ { s = ($0 ~ /^[[:space:]]*\[all\]/); next }
        s && $0 ~ re { f = 1 }
        END { exit !f }' "$CONFIG_TXT"
}
_cfg_all_append() {  # <line> <comment> — append under an explicit [all] section
    printf '\n[all]\n# Added by setup-hil-host.sh: %s\n%s\n' "$2" "$1" >> "$CONFIG_TXT"
}
if [[ -n "${HIL_USE_DWC2:-}" ]]; then
    USE_DWC2="$HIL_USE_DWC2"
elif [[ "$IS_ZERO" == "1" ]]; then
    USE_DWC2=1
    echo "  Pi Zero detected (${MODEL:-?}) — enabling dwc2 (recommended: Wi-Fi-only, no USB Ethernet at risk)"
else
    USE_DWC2=0
fi
if [[ "$USE_DWC2" == "1" ]]; then
    if [[ -f "$CONFIG_TXT" ]]; then
        if _cfg_all_has '^[[:space:]]*dtoverlay=dwc2'; then
            echo "  dwc2 overlay already present in $CONFIG_TXT"
        else
            _cfg_all_append "dtoverlay=dwc2,dr_mode=host" "mainline USB driver (robust + rebindable)"
            echo "  dwc2 overlay appended under [all] in $CONFIG_TXT — REBOOT required to take effect"
        fi
    else
        echo "  WARNING: no config.txt found (/boot/firmware or /boot) — skipping dwc2" >&2
    fi
fi

# Filesystem auto-repair on boot — Raspberry Pi (SD/eMMC) hosts ONLY. HIL hosts take
# hard power events: the controller power-cycles a wedged host, bench power is pulled,
# a DUT solenoid storms the bus. Those leave the ext4 rootfs dirty, and the stock
# cmdline drops to a read-only emergency/maintenance shell instead of repairing it —
# the host then never rejoins the network and looks permanently "unreachable" to the
# bench. `fsck.mode=force fsck.repair=yes` makes systemd-fsck check + auto-repair every
# boot so the host self-heals. Matches the rpi-displays fix. Pi-only (Particle Tachyon
# and non-Pi SBCs boot differently), and WARNED with a 3s grace so an operator watching
# an accidental re-run can Ctrl-C before cmdline.txt is edited. Needs a reboot to apply.
case "$MODEL" in
  *"Raspberry Pi"*)
    CMDLINE=/boot/firmware/cmdline.txt; [[ -f "$CMDLINE" ]] || CMDLINE=/boot/cmdline.txt
    if [[ -f "$CMDLINE" ]] && ! grep -qw "fsck.mode=force" "$CMDLINE"; then
        echo "  !! Adding 'fsck.mode=force fsck.repair=yes' to $CMDLINE so a dirty rootfs"
        echo "  !! auto-repairs on boot instead of hanging in read-only maintenance mode."
        echo "  !! Ctrl-C within 3s to skip..."
        sleep 3
        # cmdline.txt MUST remain a SINGLE line. Prefer inserting before an existing
        # fsck.repair=yes; otherwise inject the pair before rootwait.
        if grep -qw "fsck.repair=yes" "$CMDLINE"; then
            sed -i "s/\bfsck\.repair=yes\b/fsck.mode=force fsck.repair=yes/" "$CMDLINE"
        else
            sed -i "s/\brootwait\b/fsck.mode=force fsck.repair=yes rootwait/" "$CMDLINE"
        fi
        echo "  fsck auto-repair enabled in $CMDLINE (REBOOT required to take effect)"
    else
        echo "  fsck auto-repair already present (or no cmdline.txt) — skipping"
    fi
    ;;
  *) echo "  fsck cmdline fix skipped (not a Raspberry Pi: ${MODEL:-unknown})" ;;
esac

# Passwordless sudo for the HIL user — REQUIRED. The bench drives the host over
# non-interactive SSH (no TTY), and several steps need root: mounting the DUT's
# USB-MSC volume for write_secrets_msc (`sudo mount`/udisksctl), solenoid GPIO, etc.
# Without it those fail with "sudo: a terminal is required to read the password".
# Raspberry Pi OS adds this for the first user via rpi-config (Pi 5 images include
# it; some Pi 4 images don't), and the bench hosts (e.g. rpi-displays) rely on it —
# so ensure it. (The scoped usbip/reboot drop-ins below are kept as belt-and-braces.)
SUDOERS_NOPASSWD=/etc/sudoers.d/010-hil-nopasswd
if [[ -f "$SUDOERS_NOPASSWD" ]] && grep -qF "$HIL_USER ALL=(ALL) NOPASSWD: ALL" "$SUDOERS_NOPASSWD"; then
    echo "  passwordless sudo already present for $HIL_USER"
else
    echo "$HIL_USER ALL=(ALL) NOPASSWD: ALL" > "$SUDOERS_NOPASSWD"
    chmod 440 "$SUDOERS_NOPASSWD"
    if visudo -cf "$SUDOERS_NOPASSWD" >/dev/null 2>&1; then
        echo "  passwordless sudo enabled for $HIL_USER ($SUDOERS_NOPASSWD)"
    else
        echo "  WARNING: $SUDOERS_NOPASSWD failed visudo — removing" >&2
        rm -f "$SUDOERS_NOPASSWD"
    fi
fi

# Install SSH authorized key
HOME_DIR="$(getent passwd "$HIL_USER" | cut -d: -f6)"
SSH_DIR="$HOME_DIR/.ssh"
AUTH_KEYS="$SSH_DIR/authorized_keys"

mkdir -p "$SSH_DIR"
chmod 700 "$SSH_DIR"
chown "$HIL_USER:$HIL_USER" "$SSH_DIR"

# Strip CR: a .pub saved on Windows (CRLF) otherwise never matches the existing line
# and gets appended again on every run.
PUBKEY="$(tr -d '\r' < "$PUBKEY_FILE")"
if grep -qF "$PUBKEY" "$AUTH_KEYS" 2>/dev/null; then
    echo "  Key already present in $AUTH_KEYS"
else
    echo "$PUBKEY" >> "$AUTH_KEYS"
    echo "  Key installed in $AUTH_KEYS"
fi
chmod 600 "$AUTH_KEYS"
chown "$HIL_USER:$HIL_USER" "$AUTH_KEYS"

# ── Flashing toolchain ─────────────────────────────────────────────────────
# The controller SSHes into a DUT host and runs these to flash/verify a board, so
# they must be present on any host that physically holds an Arduino/MCU DUT:
#   esptool   — ESP32/ESP8266 flasher (python3 -m esptool)
#   udisks2   — udisksctl, to mount a DUT's USB-MSC volume (write_secrets_msc stage)
#   usbip     — usbip/usbipd (per-phase flashing + Discover-busids paths)
# MUST run before the usbip sudoers/modules/usbipd-service sections below, which
# expect the usbip binaries present. Idempotent + best-effort: each install is
# skipped when present and a failure WARNS rather than aborting (a USB-server-only
# host that never flashes locally may not need esptool/udisks).
export DEBIAN_FRONTEND=noninteractive
_apt_ready=0
_apt_install() {  # idempotent apt install; refreshes the cache once on first use
    if [ "$_apt_ready" = 0 ]; then apt-get update -qq >/dev/null 2>&1 || true; _apt_ready=1; fi
    apt-get install -y -qq "$@" >/dev/null 2>&1
}
echo "Flashing toolchain:"
# esptool: pip ONLY, never apt. The Debian/Pi apt `esptool` package ships WITHOUT
# the stub_flasher data, so it imports + detects the chip but run_stub() dies with
# `FileNotFoundError: .../stub_flasher/stub_flasher_*.json` — it can't actually
# flash. So verify the STUB DATA is present (not just that the module imports), and
# if missing, drop any broken apt esptool and pip-install the complete package.
if python3 -c "import esptool,glob,os,sys; p=os.path.dirname(esptool.__file__); sys.exit(0 if glob.glob(p+'/**/stub_flasher*',recursive=True) else 1)" 2>/dev/null; then
    echo "  esptool already present + complete ($(python3 -m esptool version 2>/dev/null | head -1))"
else
    apt-get remove -y -qq esptool >/dev/null 2>&1 || true   # remove broken apt esptool if present
    # Fresh Pi OS Lite (trixie) images have no pip at all.
    command -v pip3 >/dev/null 2>&1 || _apt_install python3-pip || true
    if pip3 install --break-system-packages -q --upgrade esptool >/dev/null 2>&1 || pip3 install -q --upgrade esptool >/dev/null 2>&1; then
        echo "  esptool installed (pip)"
    else
        echo "  WARNING: could not pip-install esptool — install manually (pip install esptool)" >&2
    fi
fi
# bossac — SAM (SAMD21/SAMD51) flasher. Boards like the PyPortal M4 / Titano and
# Feather/Metro M0/M4 enter the bootloader via a 1200-baud double-tap. The
# RECOMMENDED SAM path on this platform is the uf2-msc flasher (copy the .uf2 onto
# the bootloader drive — no extra tool); bossac is the alternative. IMPORTANT:
# Debian's apt `bossa-cli` (ShumaTech BOSSA 1.9.1) is BROKEN for SAMD51 writes
# (writeBuffer -> "SAM-BA operation failed"), so we install the Adafruit/Arduino
# *fork* bossac (1.9.1-arduino2), arch-matched, at /usr/local/bin/bossac (ahead of
# any apt copy on PATH). Best-effort like the rest of the toolchain.
_install_arduino_bossac() {   # -> 0 on success, 1 if no arch build / fetch failed
    local arch url sha tmp bin
    arch="$(uname -m)"
    case "$arch" in
        aarch64|arm64)
            url="https://downloads.arduino.cc/tools/bossac-1.9.1-arduino2-linuxaarch64.tar.gz"
            sha="c167fa0ea223966f4d21f5592da3888bcbfbae385be6c5c4e41f8abff35f5cb1" ;;
        armv7l|armv6l|armhf)
            url="https://downloads.arduino.cc/tools/bossac-1.9.1-arduino2-linuxarm.tar.gz"
            sha="c9539d161d23231b5beb1d09a71829744216c7f5bc2857a491999c3e567f5b19" ;;
        x86_64|amd64)
            url="https://downloads.arduino.cc/tools/bossac-1.9.1-arduino2-linux64.tar.gz"
            sha="" ;;   # not pinned (HIL flash hosts are ARM); verify-by-run below
        *) return 1 ;;
    esac
    tmp="$(mktemp -d)"
    curl -fsSL "$url" -o "$tmp/b.tgz" || { rm -rf "$tmp"; return 1; }
    if [ -n "$sha" ] && ! echo "$sha  $tmp/b.tgz" | sha256sum -c - >/dev/null 2>&1; then
        echo "  WARNING: arduino bossac sha256 mismatch — not installing" >&2; rm -rf "$tmp"; return 1
    fi
    tar -xzf "$tmp/b.tgz" -C "$tmp" 2>/dev/null || { rm -rf "$tmp"; return 1; }
    bin="$(find "$tmp" -name bossac -type f 2>/dev/null | head -1)"
    [ -n "$bin" ] || { rm -rf "$tmp"; return 1; }
    install -m 0755 "$bin" /usr/local/bin/bossac
    rm -rf "$tmp"
    /usr/local/bin/bossac --help >/dev/null 2>&1   # verify it runs on this host
}
if [ -x /usr/local/bin/bossac ] && /usr/local/bin/bossac --help >/dev/null 2>&1; then
    echo "  arduino-fork bossac already present (/usr/local/bin/bossac, $(/usr/local/bin/bossac --help 2>&1 | sed -n 2p))"
elif _install_arduino_bossac; then
    echo "  arduino-fork bossac installed -> /usr/local/bin/bossac ($(/usr/local/bin/bossac --help 2>&1 | sed -n 2p))"
else
    echo "  WARNING: no arduino-fork bossac for $(uname -m); use the uf2-msc flasher for SAM boards" >&2
    command -v bossac >/dev/null 2>&1 || _apt_install bossa-cli || true   # last resort (SAMD21 only)
fi
if command -v udisksctl >/dev/null 2>&1; then
    echo "  udisksctl already present"
elif _apt_install udisks2; then
    echo "  udisks2 installed (apt)"
else
    echo "  WARNING: could not install udisks2 (udisksctl) — needed for write_secrets_msc" >&2
fi
if command -v usbip >/dev/null 2>&1 || [ -x /usr/sbin/usbip ]; then
    echo "  usbip already present"
elif _apt_install usbip || _apt_install linux-tools-generic; then
    echo "  usbip installed (apt)"
else
    echo "  WARNING: could not install usbip — install 'usbip'/'linux-tools' manually" >&2
fi
# socat — the serial capture streams the DUT's serial port through it; without it
# serial.log is silently empty (every stream attempt fails + retries). Needed on
# any host that captures serial (i.e. holds a DUT).
if command -v socat >/dev/null 2>&1; then
    echo "  socat already present"
elif _apt_install socat; then
    echo "  socat installed (apt)"
else
    echo "  WARNING: could not install socat — serial capture (serial.log) needs it" >&2
fi
# i2c-tools — i2cdetect/i2cset for the solenoid hub (MCP23017) + I2C peripherals.
# Needed on any host with a solenoid-controlled USB hub. Harmless elsewhere. Also
# ensure the I2C bus is enabled so /dev/i2c-1 exists for the Blinka driver.
if command -v i2cdetect >/dev/null 2>&1; then
    echo "  i2c-tools already present"
elif _apt_install i2c-tools; then
    echo "  i2c-tools installed (apt)"
else
    echo "  WARNING: could not install i2c-tools — solenoid hub control needs it" >&2
fi
if command -v raspi-config >/dev/null 2>&1; then
    raspi-config nonint do_i2c 0 2>/dev/null && echo "  I2C bus enabled (raspi-config)" || true
fi
modprobe i2c-dev 2>/dev/null || true
# git + gh — for host-side per-session repo clones (e.g. protomq / firmware sources)
# with a supplied PAT. git is in the base repos; gh (GitHub CLI) is not, so add the
# official GitHub CLI apt repo first.
if command -v git >/dev/null 2>&1; then
    echo "  git already present"
elif _apt_install git; then
    echo "  git installed (apt)"
else
    echo "  WARNING: could not install git" >&2
fi
if command -v gh >/dev/null 2>&1; then
    echo "  gh already present"
else
    install -m 0755 -d /etc/apt/keyrings 2>/dev/null || true
    KEYRING=/etc/apt/keyrings/githubcli-archive-keyring.gpg
    if curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o "$KEYRING" 2>/dev/null \
       || wget -qO "$KEYRING" https://cli.github.com/packages/githubcli-archive-keyring.gpg 2>/dev/null; then
        chmod go+r "$KEYRING"
        echo "deb [arch=$(dpkg --print-architecture) signed-by=$KEYRING] https://cli.github.com/packages stable main" \
            > /etc/apt/sources.list.d/github-cli.list
        _apt_ready=0  # force an apt refresh so the new repo is seen
        if _apt_install gh; then echo "  gh installed (GitHub CLI apt repo)"; else echo "  WARNING: gh install failed" >&2; fi
    else
        echo "  WARNING: could not fetch GitHub CLI apt key — install gh manually" >&2
    fi
fi
echo ""

# Repo root next to this script (for deploying bundled tools below). When the script
# is run from a clone (the normal case) this resolves; standalone copies just skip.
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd || true)"

# ── Solenoid USB-hub power control (opt-in: HIL_SOLENOID_HUB=1) ──────────────────
# Hosts with an Adafruit 8-channel solenoid driver (#6318, an MCP23017) toggling the
# soft-latching power buttons on a USB hub, for on-demand DUT power + wedged-board
# recovery. Installs the Blinka stack and deploys the CLI the controller's
# SolenoidHubAdapter shells out to (/opt/hil/solenoid_hub_cli.py + usb_hub.py).
# Address defaults to 0x20; set HIL_SOLENOID_I2C_ADDRESS to match the A0/A1/A2 jumpers.
if [[ "${HIL_SOLENOID_HUB:-0}" == "1" ]]; then
    echo "Solenoid hub tooling (HIL_SOLENOID_HUB=1, addr ${HIL_SOLENOID_I2C_ADDRESS:-0x20}):"
    install -d -m 0755 /opt/hil
    # Blinka + MCP23017 in a DEDICATED venv (NOT pip --break-system-packages).
    # --system-site-packages so it can still see apt-provided libs (e.g. RPi.GPIO/lgpio).
    # solenoid_hub_cli.py auto-re-execs under this venv, so the controller keeps calling
    # plain `python3 /opt/hil/solenoid_hub_cli.py` — no controller-side python-path config.
    SOLENOID_VENV=/opt/hil/venv
    if [[ ! -x "$SOLENOID_VENV/bin/python" ]]; then
        apt-get install -y -qq python3-venv >/dev/null 2>&1 || true
        python3 -m venv --system-site-packages "$SOLENOID_VENV" 2>/dev/null \
            && echo "  created venv $SOLENOID_VENV (--system-site-packages)" \
            || echo "  WARNING: could not create $SOLENOID_VENV" >&2
    fi
    # --ignore-installed is REQUIRED: with --system-site-packages, a plain
    # `pip install adafruit-blinka` sees a system/apt copy as "already satisfied"
    # and installs NOTHING into the venv — the venv then silently relies on the
    # system copy and breaks the moment it's removed/changed. --ignore-installed
    # forces Blinka + its deps INTO the venv so it is self-contained.
    if [[ -x "$SOLENOID_VENV/bin/pip" ]]; then
        "$SOLENOID_VENV/bin/pip" install -q --upgrade --ignore-installed \
            adafruit-blinka adafruit-circuitpython-mcp230xx >/dev/null 2>&1 || true
    fi
    # Verify the import resolves from INSIDE the venv — fail loudly (no suppressed
    # errors), because a silent miss here is exactly what made the CLI no-op before.
    if "$SOLENOID_VENV/bin/python" - <<'PYVERIFY'
import board, busio, digitalio
from adafruit_mcp230xx.mcp23017 import MCP23017
assert "/opt/hil/venv" in board.__file__, f"Blinka not in venv (resolves to {board.__file__})"
print(f"  Blinka + MCP23017 verified in venv ({board.__file__})")
PYVERIFY
    then :; else
        echo "  ERROR: solenoid venv is missing/not-self-contained Blinka — hub control WILL FAIL" >&2
    fi
    if [[ -n "$SRC_DIR" && -f "$SRC_DIR/scripts/solenoid_hub_cli.py" ]]; then
        install -m 0755 "$SRC_DIR/scripts/solenoid_hub_cli.py" /opt/hil/solenoid_hub_cli.py
        install -m 0644 "$SRC_DIR/vendor/hil-detection/usb_hub.py" /opt/hil/usb_hub.py
        echo "  solenoid CLI + driver deployed to /opt/hil"
        # Operator bench scripts (rpi-displays layout): ~/turn_on.sh <ch>, ~/turn_off.sh <ch>,
        # ~/all_on.sh, ~/all_off.sh -> /opt/hil. turn_*.sh find the CLI next to their real path.
        install -m 0755 "$SRC_DIR/scripts/turn_on.sh" /opt/hil/turn_on.sh
        install -m 0755 "$SRC_DIR/scripts/turn_off.sh" /opt/hil/turn_off.sh
        SOLENOID_CHANNELS="${HIL_SOLENOID_CHANNELS:-0 1 2 3 4 5 6 7}"
        printf '%s\n' '#!/usr/bin/env bash' '# Managed by setup-hil-host.sh — send OFF to every solenoid-hub channel.' \
            'exec python3 /opt/hil/solenoid_hub_cli.py all_off "$@"' > /opt/hil/all_off.sh
        printf '%s\n' '#!/usr/bin/env bash' '# Managed by setup-hil-host.sh — pulse every solenoid-hub channel ON.' \
            "for ch in $SOLENOID_CHANNELS; do /opt/hil/turn_on.sh \"\$ch\"; sleep 0.3; done" > /opt/hil/all_on.sh
        chmod 0755 /opt/hil/all_on.sh /opt/hil/all_off.sh
        for s in turn_on turn_off all_on all_off; do
            ln -sfn "/opt/hil/$s.sh" "$HOME_DIR/$s.sh"
            chown -h "$HIL_USER:$HIL_USER" "$HOME_DIR/$s.sh"
        done
        echo "  bench scripts: ~/turn_on.sh ~/turn_off.sh ~/all_on.sh ~/all_off.sh -> /opt/hil (channels: $SOLENOID_CHANNELS)"
    else
        echo "  WARNING: solenoid_hub_cli.py not found next to this script — deploy /opt/hil manually" >&2
    fi
    command -v i2cdetect >/dev/null 2>&1 && { echo "  i2c bus 1:"; i2cdetect -y 1 2>/dev/null | sed 's/^/    /'; }
fi

# ── CSI / USB camera snapshot server (opt-in: HIL_CAMERA_SERVER=1) ──────────────
# Hosts with a camera watching the DUTs. Serves JPEG snapshots + MJPEG on :8080 so the
# controller's IPCamera source reads http://<host>:8080/. picamera2 backend for Pi CSI
# sensors (libcamera), v4l2/opencv for UVC webcams (server picks via --backend auto).
# NeoPixel illuminator is off by default (needs root + a ring); HIL_CAMERA_NEOPIXEL=1
# to enable. See tools/camera-server/README.md.
if [[ "${HIL_CAMERA_SERVER:-0}" == "1" ]]; then
    echo "Camera snapshot server (HIL_CAMERA_SERVER=1):"
    _apt_install python3-picamera2 && echo "  python3-picamera2 installed (Pi CSI backend)" \
        || echo "  (python3-picamera2 not installed — UVC-only host falls back to v4l2)"
    _apt_install python3-opencv v4l-utils >/dev/null 2>&1 || true
    CAM_DST="$HOME_DIR/hil-camera-server"
    if [[ -n "$SRC_DIR" && -d "$SRC_DIR/tools/camera-server" ]]; then
        install -d -m 0755 "$CAM_DST"
        cp -r "$SRC_DIR/tools/camera-server/." "$CAM_DST/" 2>/dev/null || true
        chown -R "$HIL_USER:$HIL_USER" "$CAM_DST"
        # Sensors the firmware cannot auto-detect (e.g. Arducam IMX519) need their overlay
        # under [all]: HIL_CAMERA_OVERLAY=imx519. Pi OS ships imx519.json WITHOUT an rpi.af
        # block ("no AF algorithm", lens never moves), so graft Arducam's AF block in too.
        if [[ -n "${HIL_CAMERA_OVERLAY:-}" ]]; then
            if _cfg_all_has "^[[:space:]]*dtoverlay=${HIL_CAMERA_OVERLAY}([,[:space:]]|$)"; then
                echo "  camera overlay ${HIL_CAMERA_OVERLAY} already present in $CONFIG_TXT"
            elif [[ -f "$CONFIG_TXT" ]]; then
                _cfg_all_append "dtoverlay=${HIL_CAMERA_OVERLAY}" "CSI camera sensor (not auto-detected)"
                echo "  camera overlay ${HIL_CAMERA_OVERLAY} appended under [all] — REBOOT required"
            fi
            if [[ "$HIL_CAMERA_OVERLAY" == "imx519" ]]; then
                python3 "$CAM_DST/tuning/imx519-af-patch.py" 2>&1 | sed 's/^/  /' \
                    || echo "  WARNING: imx519 AF tuning patch failed" >&2
            fi
        fi
        # NeoPixel illuminator (HIL_CAMERA_NEOPIXEL=1): rpi_ws281x drives PWM/DMA, which
        # needs root, so the unit runs as root from the /opt/hil venv (system site packages
        # keep apt's picamera2 importable). Pin/count/cap are per host; the cap
        # (0..255, e.g. 102 = 40%) is enforced server-side whatever a client requests.
        CAM_USER="$HIL_USER"; CAM_PY=/usr/bin/python3; NP_ARG="--no-neopixel"
        if [[ "${HIL_CAMERA_NEOPIXEL:-0}" == "1" ]]; then
            NP_PIN="${HIL_NEOPIXEL_PIN:-D18}"
            NP_VENV=/opt/hil/venv
            if [[ ! -x "$NP_VENV/bin/python" ]]; then
                install -d -m 0755 /opt/hil
                apt-get install -y -qq python3-venv >/dev/null 2>&1 || true
                python3 -m venv --system-site-packages "$NP_VENV" 2>/dev/null || true
            fi
            # --ignore-installed for the same reason as the solenoid venv above.
            "$NP_VENV/bin/pip" install -q --upgrade --ignore-installed \
                adafruit-blinka adafruit-circuitpython-neopixel rpi_ws281x >/dev/null 2>&1 \
                && echo "  neopixel + rpi_ws281x installed in $NP_VENV" \
                || echo "  WARNING: could not install neopixel libs into $NP_VENV" >&2
            CAM_USER=root; CAM_PY="$NP_VENV/bin/python"
            NP_ARG="--neopixel-pin $NP_PIN --neopixel-count ${HIL_NEOPIXEL_COUNT:-32} --max-brightness ${HIL_NEOPIXEL_MAX_BRIGHTNESS:-255}"
            # PWM pins share the PWM block with the onboard analog audio driver.
            case "$NP_PIN" in
              D12|D13|D18|D19)
                if [[ -f "$CONFIG_TXT" ]] && grep -qE '^[[:space:]]*dtparam=audio=on' "$CONFIG_TXT"; then
                    sed -i 's/^\([[:space:]]*\)dtparam=audio=on/\1dtparam=audio=off  # setup-hil-host.sh: NeoPixel on PWM pin/' "$CONFIG_TXT"
                    echo "  onboard audio disabled (NeoPixel on PWM pin $NP_PIN) — REBOOT required"
                fi ;;
            esac
            echo "  NeoPixel illuminator: pin $NP_PIN, ${HIL_NEOPIXEL_COUNT:-32} px, max brightness ${HIL_NEOPIXEL_MAX_BRIGHTNESS:-255}/255"
        fi
        cat > /etc/systemd/system/hil-camera.service <<EOF
# Managed by setup-hil-host.sh — HIL camera snapshot server (IPCamera source on :8080).
[Unit]
Description=HIL Camera Snapshot Server
After=network.target

[Service]
Type=simple
User=$CAM_USER
WorkingDirectory=$CAM_DST
ExecStart=$CAM_PY $CAM_DST/server.py --port 8080 $NP_ARG
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
        systemctl daemon-reload
        # restart, not just `enable --now`: on a re-run the service is already up and
        # would keep serving the OLD code and unit until the next reboot.
        systemctl enable hil-camera 2>/dev/null && systemctl restart hil-camera 2>/dev/null \
            && echo "  hil-camera.service (re)started (snapshots on :8080)" \
            || echo "  WARNING: could not start hil-camera.service — check 'journalctl -u hil-camera'" >&2
    else
        echo "  WARNING: tools/camera-server not found next to this script — deploy manually" >&2
    fi
fi

# usbip — passwordless sudo + kernel modules for per-phase flashing.
# arduino-ws jobs with flash_mode=usbip have the controller (client) attach a
# DUT's USB port that physically lives on a server host, then flash it. Both
# sides call usbip via sudo without a TTY, so a no-prompt sudoers drop-in is
# required. We provision both roles (harmless if a host only acts as one):
#   server (USB-host, e.g. rpi-displays): usbipd + `usbip bind/unbind`
#   client (controller, e.g. tachyon):    vhci-hcd + `usbip attach/detach/port`
USBIP_BIN="$(command -v usbip || echo /usr/sbin/usbip)"
SUDOERS_USBIP=/etc/sudoers.d/hil-usbip
MODPROBE_BIN="$(command -v modprobe || echo /usr/sbin/modprobe)"
cat > "$SUDOERS_USBIP" <<EOF
# Managed by setup-hil-host.sh — passwordless usbip for HIL per-phase flashing.
$HIL_USER ALL=(root) NOPASSWD: $USBIP_BIN, $MODPROBE_BIN vhci-hcd, $MODPROBE_BIN usbip-host
EOF
chmod 440 "$SUDOERS_USBIP"
if visudo -cf "$SUDOERS_USBIP" >/dev/null 2>&1; then
    echo "  usbip sudoers drop-in written to $SUDOERS_USBIP"
else
    echo "  WARNING: $SUDOERS_USBIP failed visudo check — removing" >&2
    rm -f "$SUDOERS_USBIP"
fi

# Load the usbip kernel modules now and persist them across reboots.
# vhci-hcd (client side) is NOT loaded at boot on Raspberry Pi DUT hosts: it registers
# two USB buses, and on a Pi whose USB controller is a module (dwc2) it can register
# first, renumbering the bench bus (1-1.x -> 3-1.x) so every stored hub_port_path /
# usbip busid stops matching. Pi DUT hosts are usbip SERVERS; the controller is the
# client. HIL_USBIP_CLIENT=1/0 overrides (default: 0 on a Raspberry Pi, else 1).
case "$MODEL" in *"Raspberry Pi"*) USBIP_CLIENT_DEFAULT=0 ;; *) USBIP_CLIENT_DEFAULT=1 ;; esac
USBIP_CLIENT="${HIL_USBIP_CLIENT:-$USBIP_CLIENT_DEFAULT}"
modprobe usbip-host 2>/dev/null && echo "  usbip-host loaded (usbip server)" || true
if [[ "$USBIP_CLIENT" == "1" ]]; then
    modprobe vhci-hcd 2>/dev/null && echo "  vhci-hcd loaded (usbip client)" || true
    echo -e "vhci-hcd\nusbip-host" > /etc/modules-load.d/hil-usbip.conf
else
    printf '%s\n' "# Managed by setup-hil-host.sh — usbip SERVER only (vhci-hcd not loaded at boot;" \
        "# it would renumber the bench USB bus). HIL_USBIP_CLIENT=1 to add it." usbip-host \
        > /etc/modules-load.d/hil-usbip.conf
fi
echo "  persisted usbip modules in /etc/modules-load.d/hil-usbip.conf (client: $USBIP_CLIENT)"

# Passwordless reboot — the controller's wedged-host auto-recovery
# (HIL_AUTO_HOST_REBOOT) SSHes in as $HIL_USER and runs `sudo reboot` to clear a
# wedged dwc_otg USB stack, which is NOT runtime-rebindable (a wedge silently
# hides DUTs and only a reboot recovers it; dwc2, set above, makes this rare but
# not impossible). Non-TTY SSH can't answer a password prompt, so a no-prompt
# sudoers drop-in is required. Scope it to just the reboot commands.
REBOOT_BIN="$(command -v reboot || echo /sbin/reboot)"
SYSTEMCTL_BIN="$(command -v systemctl || echo /usr/bin/systemctl)"
SUDOERS_REBOOT=/etc/sudoers.d/hil-reboot
cat > "$SUDOERS_REBOOT" <<EOF
# Managed by setup-hil-host.sh — passwordless reboot for HIL wedged-host recovery.
$HIL_USER ALL=(root) NOPASSWD: $REBOOT_BIN, $SYSTEMCTL_BIN reboot
EOF
chmod 440 "$SUDOERS_REBOOT"
if visudo -cf "$SUDOERS_REBOOT" >/dev/null 2>&1; then
    echo "  reboot sudoers drop-in written to $SUDOERS_REBOOT"
else
    echo "  WARNING: $SUDOERS_REBOOT failed visudo check — removing" >&2
    rm -f "$SUDOERS_REBOOT"
fi

# Start usbipd on USB-server hosts. usbipd MUST be running (listening on
# :3240) on the host that physically holds the DUT, or the controller's
# `usbip attach` fails with "usbip: error: tcp connect". Prefer a packaged
# usbipd.service; otherwise install our own unit so it survives reboots —
# Debian/RPi's `usbip` package ships the binary but no service unit, so the
# manual `sudo usbipd -D` you'd otherwise need does not persist.
USBIPD_BIN="$(command -v usbipd || echo /usr/sbin/usbipd)"
if systemctl list-unit-files 2>/dev/null | grep -q '^usbipd\.service'; then
    systemctl enable --now usbipd 2>/dev/null \
        && echo "  usbipd.service enabled+started" \
        || echo "  WARNING: could not enable usbipd.service" >&2
elif [ -x "$USBIPD_BIN" ]; then
    cat > /etc/systemd/system/hil-usbipd.service <<EOF
# Managed by setup-hil-host.sh — usbip host daemon for HIL per-phase flashing.
# Needed only on USB-server hosts (those physically holding DUTs).
[Unit]
Description=usbip host daemon (HIL per-phase flashing)
After=network.target

[Service]
ExecStart=$USBIPD_BIN
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now hil-usbipd 2>/dev/null \
        && echo "  hil-usbipd.service installed+started (listens :3240)" \
        || echo "  WARNING: could not enable hil-usbipd.service" >&2
else
    echo "  usbipd not found (install 'usbip'/'linux-tools' on USB-server hosts)"
fi

# NOTE: keep the blanket vendor/usbip-autoattach autobind rule OFF — per-phase
# flashing binds only the single leased busid, on demand.

# Disk-backed /tmp/hil — job work dirs live under /tmp/hil/<job>, but /tmp is a small
# RAM tmpfs on Pi OS (~200 MB on a Zero 2 W), too small for a firmware workspace.
# Bind /tmp/hil onto ~/hil-work. HIL_WORK_ON_DISK=1/0 overrides (default: on when the
# host has < 1 GiB RAM).
MEM_KB="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)"
WORK_ON_DISK="${HIL_WORK_ON_DISK:-$([[ "${MEM_KB:-0}" -lt 1048576 ]] && echo 1 || echo 0)}"
if [[ "$WORK_ON_DISK" == "1" ]]; then
    install -d -o "$HIL_USER" -g "$HIL_USER" -m 1777 "$HOME_DIR/hil-work"
    cat > /etc/systemd/system/tmp-hil.mount <<EOF
# Managed by setup-hil-host.sh — disk-backed /tmp/hil for HIL job work dirs.
[Unit]
Description=Disk-backed /tmp/hil for HIL job work dirs
After=tmp.mount

[Mount]
What=$HOME_DIR/hil-work
Where=/tmp/hil
Type=none
Options=bind

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now tmp-hil.mount 2>/dev/null \
        && echo "  /tmp/hil bind-mounted on $HOME_DIR/hil-work (tmp-hil.mount)" \
        || echo "  WARNING: could not enable tmp-hil.mount" >&2
fi

echo ""
echo "Done. Current groups for $HIL_USER:"
id "$HIL_USER"
echo ""
echo "Log out and back in (or run 'newgrp <group>') for group changes to take effect."
