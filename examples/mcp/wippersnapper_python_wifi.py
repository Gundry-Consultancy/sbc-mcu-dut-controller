"""Real Pi Wi-Fi + WipperSnapper check-in smoke test.

Requires Ethernet management, an initially disconnected wlan0, NetworkManager,
passwordless sudo for nmcli, and WipperSnapper installed in the interpreter.
The ping is forced over Wi-Fi; MQTT uses the normal host route.
"""

import asyncio
import json
import os
import subprocess
import uuid

from Adafruit_WipperSnapper_Python.client import Client

name = "hil-mcp-smoke-" + uuid.uuid4().hex[:8]


def nm(*args):
    result = subprocess.run(
        ["sudo", "nmcli", *args], check=False, capture_output=True, text=True
    )
    if result.returncode:
        # CalledProcessError would include password-bearing argv in its traceback.
        raise RuntimeError("NetworkManager operation failed; check the bench configuration")
    return result.stdout


async def main():
    try:
        nm(
            "con",
            "add",
            "type",
            "wifi",
            "ifname",
            "wlan0",
            "con-name",
            name,
            "ssid",
            os.environ["WIFI_SSID"],
            "wifi-sec.key-mgmt",
            "wpa-psk",
            "wifi-sec.psk",
            os.environ["WIFI_PASSWORD"],
            "ipv4.method",
            "auto",
            "ipv4.never-default",
            "yes",
            "ipv6.method",
            "disabled",
            "connection.autoconnect",
            "no",
        )
        nm("con", "up", name)
        address = json.loads(subprocess.check_output(["ip", "-j", "-4", "addr", "show", "wlan0"]))[
            0
        ]["addr_info"][0]["local"]
        subprocess.run(
            ["ping", "-I", "wlan0", "-c", "2", "-W", "3", os.environ["MQTT_HOST"]], check=True
        )
        print("MCP_PY_WIFI_OK", address, os.environ["WIFI_SSID"], flush=True)
        client = Client(
            os.environ["IO_USERNAME"],
            os.environ["IO_KEY"],
            hostname=os.environ["MQTT_HOST"],
            port=int(os.environ["MQTT_PORT"]),
        )
        client.run_async()
        try:
            await asyncio.wait_for(client.checked_in, 60)
            print("MCP_PY_CHECKIN_OK", flush=True)
        finally:
            await client.stop()
    finally:
        subprocess.run(["sudo", "nmcli", "con", "delete", name], check=False, capture_output=True)


asyncio.run(main())
