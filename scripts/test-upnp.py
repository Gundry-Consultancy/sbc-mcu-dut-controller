#!/usr/bin/env python3
"""Manual integration test for UPnP IGD port mapping.

Speaks SOAP directly so it works from WSL2 where SSDP multicast can't
reach the real LAN.  Discovers the IGD description from a known URL,
then exercises add / query / reclaim / delete against the real router.

Usage:
    # auto-discover via SSDP (works on a real LAN)
    python scripts/test-upnp.py

    # bypass SSDP (WSL2, Docker, etc.) — local IP is detected automatically
    python scripts/test-upnp.py \
        --igd-url http://192.168.1.1:49152/<uuid>/gatedesc.xml \
        [--port 8080]

    # WSL2: routing table returns the virtual NAT IP, not the Windows LAN IP;
    # override it explicitly in that case:
    python scripts/test-upnp.py \
        --igd-url http://192.168.1.1:49152/<uuid>/gatedesc.xml \
        --local-ip 192.168.1.207

Exits 0 on success, 1 on any failure.
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
import xml.etree.ElementTree as ET
from textwrap import dedent
from urllib.parse import urlparse

import miniupnpc

from hil_controller.upnp import local_ip_toward

_NS = "urn:schemas-upnp-org:device-1-0"


def _check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    if not condition:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Thin SOAP client — used when miniupnpc discovery is not available
# ---------------------------------------------------------------------------

class _IGD:
    """Minimal UPnP IGD SOAP client."""

    def __init__(self, description_url: str) -> None:
        xml_bytes = urllib.request.urlopen(description_url, timeout=5).read()
        root = ET.fromstring(xml_bytes)
        # Origin = scheme + host + port (control URLs are absolute paths from root)
        parsed = urlparse(description_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        igd_host = parsed.hostname or parsed.netloc.split(":")[0]
        self.local_ip: str = local_ip_toward(igd_host)

        # Prefer WANIPConnection, fall back to WANPPPConnection
        self._service_type: str | None = None
        self._control_url: str | None = None
        for svc in root.findall(f".//{{{_NS}}}service"):
            st = svc.findtext(f"{{{_NS}}}serviceType") or ""
            if "WANIPConnection" in st or "WANPPPConnection" in st:
                ctrl = svc.findtext(f"{{{_NS}}}controlURL") or ""
                if not self._service_type or "WANIPConnection" in st:
                    self._service_type = st.strip()
                    self._control_url = origin + "/" + ctrl.lstrip("/")
        if not self._control_url:
            raise RuntimeError("No WANIPConnection or WANPPPConnection service found in IGD description")
        print(f"  Service : {self._service_type}")
        print(f"  Control : {self._control_url}")

        # Get external IP via SOAP (use wildcard namespace match)
        resp = self._soap("GetExternalIPAddress", "")
        ip_el = ET.fromstring(resp).find(".//{*}NewExternalIPAddress")
        self.external_ip: str = ip_el.text.strip() if ip_el is not None and ip_el.text else "unknown"

    def _soap(self, action: str, body_inner: str) -> str:
        assert self._service_type and self._control_url
        envelope = dedent(f"""\
            <?xml version="1.0"?>
            <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
                        s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
              <s:Body>
                <u:{action} xmlns:u="{self._service_type}">
                  {body_inner}
                </u:{action}>
              </s:Body>
            </s:Envelope>""").encode()
        req = urllib.request.Request(
            self._control_url,
            data=envelope,
            headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPAction": f'"{self._service_type}#{action}"',
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.read().decode()
        except urllib.error.HTTPError as e:
            return e.read().decode()

    def addportmapping(self, ext_port: int, proto: str, int_ip: str, int_port: int, desc: str, lease: int) -> bool:
        body = dedent(f"""\
            <NewRemoteHost></NewRemoteHost>
            <NewExternalPort>{ext_port}</NewExternalPort>
            <NewProtocol>{proto}</NewProtocol>
            <NewInternalPort>{int_port}</NewInternalPort>
            <NewInternalClient>{int_ip}</NewInternalClient>
            <NewEnabled>1</NewEnabled>
            <NewPortMappingDescription>{desc}</NewPortMappingDescription>
            <NewLeaseDuration>{lease}</NewLeaseDuration>""")
        resp = self._soap("AddPortMapping", body)
        return "errorCode" not in resp

    def deleteportmapping(self, ext_port: int, proto: str) -> bool:
        body = dedent(f"""\
            <NewRemoteHost></NewRemoteHost>
            <NewExternalPort>{ext_port}</NewExternalPort>
            <NewProtocol>{proto}</NewProtocol>""")
        resp = self._soap("DeletePortMapping", body)
        return "errorCode" not in resp

    def getspecificportmapping(self, ext_port: int, proto: str) -> tuple | None:
        body = dedent(f"""\
            <NewRemoteHost></NewRemoteHost>
            <NewExternalPort>{ext_port}</NewExternalPort>
            <NewProtocol>{proto}</NewProtocol>""")
        resp = self._soap("GetSpecificPortMappingEntry", body)
        root = ET.fromstring(resp)
        # 714 = NoSuchEntryInArray — mapping not present
        err = root.find(".//{urn:schemas-xmlsoap-org:soap:encoding/}errorCode")
        if err is None:
            err = root.find(".//{http://schemas.xmlsoap.org/soap/envelope/}Fault")
        if "714" in resp or ("errorCode" in resp and "714" in resp):
            return None
        if "errorCode" in resp:
            return None
        client_el = root.find(".//{*}NewInternalClient")
        port_el = root.find(".//{*}NewInternalPort")
        if client_el is None:
            return None
        return (
            (client_el.text or "").strip(),
            int((port_el.text or "0").strip()),
        )


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--igd-url", default=None,
                        help="Skip SSDP; use this IGD description URL directly")
    parser.add_argument("--local-ip", default=None,
                        help="Override detected LAN IP (needed on WSL2)")
    args = parser.parse_args()
    port: int = args.port

    if args.igd_url:
        print(f"Using IGD URL directly (no SSDP): {args.igd_url}")
        igd = _IGD(args.igd_url)
        local_ip: str = args.local_ip or igd.local_ip
        print(f"  LAN={local_ip}  WAN={igd.external_ip}")
        _run_tests(igd, port, local_ip)
    else:
        u = miniupnpc.UPnP()
        u.discoverdelay = 500
        print("Discovering IGD devices via SSDP...")
        n = u.discover()
        _check(f"at least one IGD found (got {n})", n > 0)
        igd_url = u.selectigd()
        igd_host = igd_url.split("//", 1)[-1].split("/")[0].split(":")[0]
        local_ip: str = local_ip_toward(igd_host)
        print(f"  LAN={local_ip}  WAN={u.externalipaddress()}")

        class _MiniupnpcAdapter:
            def addportmapping(self, ep, pr, ii, ip, d, l):  # noqa: E741
                return u.addportmapping(ep, pr, ii, ip, d, l)
            def deleteportmapping(self, ep, pr):
                return u.deleteportmapping(ep, pr)
            def getspecificportmapping(self, ep, pr):
                r = u.getspecificportmapping(ep, pr)
                return (r[0], r[1]) if r is not None else None

        _run_tests(_MiniupnpcAdapter(), port, local_ip)


def _run_tests(igd: object, port: int, local_ip: str) -> None:
    # --- baseline: clear any pre-existing mapping ---
    existing = igd.getspecificportmapping(port, "TCP")
    if existing:
        print(f"  Pre-existing mapping: {existing} — clearing")
        igd.deleteportmapping(port, "TCP")
        _check("pre-existing mapping removed", igd.getspecificportmapping(port, "TCP") is None)

    # --- add mapping ---
    print(f"\nAdding mapping: external {port} -> {local_ip}:{port}")
    ok = igd.addportmapping(port, "TCP", local_ip, port, "hil-controller-test", 120)
    _check("addportmapping returned success", bool(ok))

    mapping = igd.getspecificportmapping(port, "TCP")
    _check("mapping visible via getspecificportmapping", mapping is not None)
    _check(f"mapping points to us ({local_ip})", mapping is not None and mapping[0] == local_ip)
    print(f"  Mapping confirmed: {mapping}")

    # --- already mapped to us: verify no delete needed ---
    print(f"\nSimulating re-open when already mapped to us...")
    existing2 = igd.getspecificportmapping(port, "TCP")
    _check("mapping still present", existing2 is not None)
    _check(f"mapping still ours ({local_ip})", existing2 is not None and existing2[0] == local_ip)
    print("  Correctly identified as ours — no delete/re-add needed")

    # --- mapped to another host: delete + re-add ---
    # linux-igd enforces per-client auth (error 606) so we can't fake a
    # mapping owned by a different host from a single test client.
    # The reclaim logic in upnp.py is covered by unit tests (test_upnp.py).
    # Here we verify the detection path only: mapping is ours → no action.
    print(f"\nReclaim scenario (per-client router): verifying own-IP detection only...")
    own = igd.getspecificportmapping(port, "TCP")
    _check(f"mapping still present and owned by us ({local_ip})",
           own is not None and own[0] == local_ip)
    print(f"  Confirmed: {own[0]} == {local_ip}, reclaim path not needed")

    # --- remove mapping ---
    print(f"\nRemoving mapping...")
    ok3 = igd.deleteportmapping(port, "TCP")
    _check("deleteportmapping returned success", bool(ok3))
    _check("mapping gone after delete", igd.getspecificportmapping(port, "TCP") is None)

    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
