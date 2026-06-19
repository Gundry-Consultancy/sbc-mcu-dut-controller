"""GET /v1/hosts, GET /v1/hosts/{id}"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from hil_controller import host_hardware
from hil_controller.auth.principal import Principal
from hil_controller.auth.tokens import require_auth
from hil_controller.db.connection import get_db

router = APIRouter(prefix="/v1/hosts", tags=["hosts"])
Auth = Annotated[Principal, Depends(require_auth)]


class HostSummary(BaseModel):
    id: str
    role: str
    addr: str
    transport: str
    status: str
    last_seen_at: str | None
    max_concurrent_jobs: int | None
    capabilities: list[str]
    device_count: int
    # Merged detected+override hardware, live load, and work-speed score.
    hardware: dict[str, Any] | None = None


class HostDetail(HostSummary):
    ssh_user: str
    devices: list[dict[str, Any]]
    recent_jobs: list[dict[str, Any]]


@router.get("", response_model=list[HostSummary])
async def list_hosts(request: Request, _auth: Auth) -> list[HostSummary]:
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        async with db.execute(
            """
            SELECT h.*, COUNT(d.id) AS device_count
            FROM hosts h
            LEFT JOIN devices d ON d.host_id = h.id
            GROUP BY h.id
            ORDER BY h.id
            """
        ) as cur:
            rows = await cur.fetchall()
    return [
        HostSummary(
            id=r["id"],
            role=r["role"],
            addr=r["addr"],
            transport=r["transport"],
            status=r["status"],
            last_seen_at=r["last_seen_at"],
            max_concurrent_jobs=r["max_concurrent_jobs"],
            capabilities=json.loads(r["capabilities_json"]),
            device_count=r["device_count"],
            hardware=host_hardware.host_hw_view(dict(r)),
        )
        for r in rows
    ]


@router.get("/{host_id}", response_model=HostDetail)
async def get_host(request: Request, host_id: str, _auth: Auth) -> HostDetail:
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        async with db.execute("SELECT * FROM hosts WHERE id = ?", (host_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Host not found")

        async with db.execute(
            "SELECT * FROM devices WHERE host_id = ? ORDER BY id", (host_id,)
        ) as cur:
            device_rows = await cur.fetchall()

        async with db.execute(
            """
            SELECT id, state, result, created_at, finished_at
            FROM jobs WHERE assigned_host = ? ORDER BY created_at DESC LIMIT 10
            """,
            (host_id,),
        ) as cur:
            job_rows = await cur.fetchall()

    return HostDetail(
        id=row["id"],
        role=row["role"],
        addr=row["addr"],
        transport=row["transport"],
        status=row["status"],
        last_seen_at=row["last_seen_at"],
        max_concurrent_jobs=row["max_concurrent_jobs"],
        capabilities=json.loads(row["capabilities_json"]),
        device_count=len(device_rows),
        hardware=host_hardware.host_hw_view(dict(row)),
        ssh_user=row["ssh_user"],
        devices=[dict(d) for d in device_rows],
        recent_jobs=[dict(j) for j in job_rows],
    )


# --------------------------------------------------------------------------- #
# usbip exportable busids                                                     #
# --------------------------------------------------------------------------- #


class UsbipExportable(BaseModel):
    busid: str
    vid: str
    pid: str
    description: str | None = None
    matched_device_id: str | None = None
    manufacturer: str | None = None
    product: str | None = None
    serial: str | None = None
    speed: str | None = None
    max_power: str | None = None
    num_interfaces: int | None = None
    device_class: str | None = None
    driver: str | None = None
    lsusb_description: str | None = None
    port_power_status: str | None = None
    port_connect_status: str | None = None
    port_status_text: str | None = None


class DevLinkEntry(BaseModel):
    name: str
    target: str


class DevLinks(BaseModel):
    """Stable /dev symlinks for serial capture + UF2/MSC flashing."""

    serial_by_id: list[DevLinkEntry] = []
    serial_by_path: list[DevLinkEntry] = []
    disk_by_id: list[DevLinkEntry] = []
    disk_by_label: list[DevLinkEntry] = []
    # USB-topology mirror; last-resort disambiguator only.
    disk_by_path: list[DevLinkEntry] = []


class UsbipExportableResponse(BaseModel):
    host_id: str
    daemon_listening: bool
    busids: list[UsbipExportable]
    error: str | None = None
    # Populated only for Arduino-tagged hosts (or hosts of arduino devices):
    # the persistent serial/disk paths a serial-capture or flashing skill
    # needs. ``null`` when the host doesn't qualify or collection failed.
    dev_links: DevLinks | None = None


@router.get("/{host_id}/usbip/exportable", response_model=UsbipExportableResponse)
async def list_exportable_busids(
    request: Request, host_id: str, _auth: Auth
) -> UsbipExportableResponse:
    """Run ``usbip list -l`` on the host and return what's exportable.

    Read-only — does not bind anything. Joins each busid against the
    ``devices`` table (matching on ``hub_port_path`` + ``hub_host_id``)
    so the UI can show "1-1.1.1.4 → mcu-feather-esp32s3-revtft".

    Returns ``daemon_listening=false`` (with the stderr blob in
    ``error``) when usbipd is down on the host — operators see that
    instead of a 500.
    """
    db_path: str = request.app.state.db_path
    async with get_db(db_path) as db:
        async with db.execute("SELECT * FROM hosts WHERE id = ?", (host_id,)) as cur:
            host_row = await cur.fetchone()
        if host_row is None:
            raise HTTPException(status_code=404, detail="Host not found")
        async with db.execute(
            "SELECT id, hub_port_path, capabilities_json FROM devices "
            "WHERE hub_host_id = ? OR (hub_host_id IS NULL AND host_id = ?)",
            (host_id, host_id),
        ) as cur:
            device_rows = await cur.fetchall()
    busid_to_device = {r["hub_port_path"]: r["id"] for r in device_rows if r["hub_port_path"]}
    # Arduino-tagged hosts (or hosts of arduino devices) also get their stable
    # /dev serial + disk symlinks, for serial-capture / flashing skills.
    wants_dev_links = "arduino" in json.loads(host_row["capabilities_json"]) or any(
        "arduino" in json.loads(r["capabilities_json"]) for r in device_rows
    )

    registry = getattr(request.app.state, "host_registry", None)
    if registry is None:
        raise HTTPException(
            status_code=503, detail="host registry not configured on this controller"
        )
    try:
        transport = registry.transport_for(host_id)
    except (KeyError, AttributeError) as exc:
        raise HTTPException(status_code=503, detail=f"no transport for host: {exc}")

    from hil_controller.adapters.usbip_inventory import query_host_busids

    inventory = await query_host_busids(
        transport,
        host_id=host_id,
        device_busid_map=busid_to_device,
        include_dev_links=wants_dev_links,
    )
    dev_links = None
    if inventory.dev_links is not None:
        dev_links = DevLinks(
            serial_by_id=[DevLinkEntry(**e) for e in inventory.dev_links.serial_by_id],
            serial_by_path=[DevLinkEntry(**e) for e in inventory.dev_links.serial_by_path],
            disk_by_id=[DevLinkEntry(**e) for e in inventory.dev_links.disk_by_id],
            disk_by_label=[DevLinkEntry(**e) for e in inventory.dev_links.disk_by_label],
            disk_by_path=[DevLinkEntry(**e) for e in inventory.dev_links.disk_by_path],
        )
    return UsbipExportableResponse(
        host_id=inventory.host_id,
        daemon_listening=inventory.daemon_listening,
        dev_links=dev_links,
        busids=[
            UsbipExportable(
                busid=b.busid,
                vid=b.vid,
                pid=b.pid,
                description=b.description or None,
                matched_device_id=b.matched_device_id,
                manufacturer=b.manufacturer,
                product=b.product,
                serial=b.serial,
                speed=b.speed,
                max_power=b.max_power,
                num_interfaces=b.num_interfaces,
                device_class=b.device_class,
                driver=b.driver,
                lsusb_description=b.lsusb_description,
                port_power_status=b.port_power_status,
                port_connect_status=b.port_connect_status,
                port_status_text=b.port_status_text,
            )
            for b in inventory.busids
        ],
        error=inventory.error,
    )
