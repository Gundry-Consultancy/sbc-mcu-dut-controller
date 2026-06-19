"""UPnP IGD port-mapping helper.

Opens (and on shutdown removes) a TCP port-mapping on the local gateway so
the HIL controller is reachable from outside the LAN without manual port
forwarding rules.

Disabled by default; set HIL_UPNP_ENABLED=true to activate.
"""

from __future__ import annotations

import asyncio
import logging
import socket

import miniupnpc  # type: ignore[import-untyped]

log = logging.getLogger(__name__)

_DESCRIPTION = "hil-controller"


def local_ip_toward(remote_host: str) -> str:
    """Return the source IP the OS would use to reach *remote_host*.

    Sends no traffic — just asks the kernel's routing table via a connected
    UDP socket, which works correctly across multiple interfaces, VPNs, etc.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((remote_host, 80))
        return s.getsockname()[0]


def _discover_and_map(external_port: int, internal_port: int, lease_seconds: int) -> str | None:
    u = miniupnpc.UPnP()
    u.discoverdelay = 200  # ms
    n = u.discover()
    if n == 0:
        log.warning("upnp: no IGD devices found")
        return None

    igd_url = u.selectigd()  # returns the IGD location URL, e.g. "http://192.168.1.1:49152/..."
    igd_host = igd_url.split("//", 1)[-1].split("/")[0].split(":")[0]
    local_ip = local_ip_toward(igd_host)
    existing = u.getspecificportmapping(external_port, "TCP")
    if existing:
        # existing is (internalIP, internalPort, desc, enabled, remoteHost, leaseDuration)
        if existing[0] == local_ip:
            log.info("upnp: port %d already mapped to us, skipping", external_port)
            return local_ip
        log.info(
            "upnp: port %d mapped to %s, reclaiming for %s",
            external_port,
            existing[0],
            local_ip,
        )
        u.deleteportmapping(external_port, "TCP")

    result = u.addportmapping(
        external_port,
        "TCP",
        local_ip,
        internal_port,
        _DESCRIPTION,
        lease_seconds,
    )
    if result:
        ext_ip = u.externalipaddress()
        log.info(
            "upnp: mapped %s:%d -> %s:%d (lease %ds)",
            ext_ip,
            external_port,
            local_ip,
            internal_port,
            lease_seconds,
        )
        return local_ip
    else:
        log.warning("upnp: addportmapping returned falsy result")
        return None


def _remove_mapping(external_port: int) -> None:
    u = miniupnpc.UPnP()
    u.discoverdelay = 200
    if u.discover() == 0:
        return
    u.selectigd()
    result = u.deleteportmapping(external_port, "TCP")
    if result:
        log.info("upnp: removed port mapping for external port %d", external_port)
    else:
        log.warning("upnp: failed to remove port mapping for external port %d", external_port)


async def open_port(
    external_port: int,
    internal_port: int,
    lease_seconds: int = 3600,
) -> str | None:
    """Async wrapper: opens the UPnP port mapping. Returns the LAN IP or None."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _discover_and_map, external_port, internal_port, lease_seconds
    )


async def close_port(external_port: int) -> None:
    """Async wrapper: removes the UPnP port mapping."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _remove_mapping, external_port)
