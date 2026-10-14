import os
import time

import wifi

wifi.radio.connect(os.getenv("CIRCUITPY_WIFI_SSID"), os.getenv("CIRCUITPY_WIFI_PASSWORD"))
assert wifi.radio.ipv4_address is not None
while True:
    print("MCP_CP_WIFI_OK", wifi.radio.ipv4_address, wifi.radio.ap_info.ssid)
    time.sleep(2)
