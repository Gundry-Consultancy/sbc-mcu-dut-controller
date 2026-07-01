"""FastAPI app factory and uvicorn entry point."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI

log = logging.getLogger(__name__)


def create_app(db_path: str | None = None, topology_file: str | None = None) -> FastAPI:
    from hil_controller.config import get_settings

    settings = get_settings()
    _db_path = db_path or settings.db_path
    _topology_file = topology_file if topology_file is not None else settings.topology_file

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from hil_controller.db.connection import init_db
        from hil_controller.queue.events import EventBus
        from hil_controller.queue.leases import startup_sweep
        from hil_controller.queue.scheduler import Scheduler
        from hil_controller.topology.seeder import seed_topology

        await init_db(_db_path)
        await seed_topology(_db_path, _topology_file)
        await startup_sweep(_db_path)

        event_bus = EventBus()

        host_registry = None
        if _topology_file:
            from hil_controller.hosts.registry import RealHostRegistry

            host_registry = RealHostRegistry(topology_file=_topology_file, db_path=_db_path)
            host_registry.load()

        scheduler = Scheduler(db_path=_db_path, event_bus=event_bus, host_registry=host_registry)
        await scheduler.start()

        # Device-availability reconciler (self-heals temporary outages). Only
        # started when a topology is configured (i.e. a real bench), so headless
        # runs and the test suite don't spin a background timer that never quits.
        reconciler = None
        if _topology_file:
            from hil_controller import host_recovery
            from hil_controller.availability_reconciler import AvailabilityReconciler

            # Reboot fn for wedged-host recovery: build the host's transport from
            # the registry and run the all_off → reboot → all_on sequence. Gated
            # by HIL_AUTO_HOST_REBOOT inside recover_wedged_hosts.
            async def _channel_nodes(host_id: str) -> list[tuple[int, str]]:
                """(solenoid_channel, by-path node) for the host's devices, channel-sorted.

                Drives the gentle sequential per-channel bring-up after a reboot (one
                channel at a time, avoiding the all_on dwc_otg storm)."""
                from hil_controller.db.connection import get_db

                rows: list[tuple[int, str]] = []
                async with get_db(_db_path) as db:
                    cur = await db.execute(
                        "SELECT solenoid_channel, serial_port FROM devices "
                        "WHERE (host_id=? OR hub_host_id=?) AND solenoid_channel IS NOT NULL",
                        (host_id, host_id),
                    )
                    for r in await cur.fetchall():
                        rows.append((int(r["solenoid_channel"]), r["serial_port"] or ""))
                # dedupe channels (multiple device rows can share one), sort for determinism
                seen: dict[int, str] = {}
                for ch, node in rows:
                    seen.setdefault(ch, node)
                return sorted(seen.items())

            async def _reboot_host(host_id: str) -> bool:
                if host_registry is None:
                    return False
                try:
                    transport = host_registry.transport_for(host_id)
                except KeyError:
                    return False
                channel_nodes = await _channel_nodes(host_id)
                return await host_recovery.reboot_host(
                    transport, channel_nodes=channel_nodes or None
                )

            # Presence probe: is the device actually back on its host? SSH to the
            # host and test that its by-path serial node exists. Lets the reconciler
            # auto-clear a temporary outage (network-unreachable host, or a wedge
            # that recovered out-of-band) once the device re-enumerates. Guarded by
            # a short timeout so a still-down host can't stall the reconcile tick.
            async def _presence_probe(device: dict) -> bool:
                # Healthy = the device's by-path node enumerates AND dmesg shows no
                # recent USB error storm. Validates a flagged device before we'd keep
                # returning it unavailable: if it's actually healthy, the reconciler
                # clears it. Under on-demand power an idle DUT is OFF, so a device with
                # a solenoid channel is validated ACTIVELY (power on → check → off);
                # a channel-less DUT (assumed always-on) is checked passively.
                if host_registry is None:
                    return False
                host_id = device.get("hub_host_id") or device.get("host_id")
                node = device.get("serial_port") or device.get("hub_port_path")
                channel = device.get("solenoid_channel")
                if not host_id or not node:
                    return False
                try:
                    transport = host_registry.transport_for(host_id)
                except KeyError:
                    return False
                if channel is not None:
                    return await host_recovery.validate_presence_active(
                        transport, channel=int(channel), node=node
                    )
                if not await host_recovery._node_present(transport, node):
                    return False
                return await host_recovery.dmesg_usb_error_count(transport) == 0

            reconciler = AvailabilityReconciler(
                db_path=_db_path, probe=_presence_probe, host_reboot_fn=_reboot_host
            )
            await reconciler.start()

        # Host hardware monitor: auto-detect each host's real board model/specs
        # (so /v1/targets stops calling every SBC "pi5") and refresh live load
        # every host_load_s. Only with a topology (real bench) + when enabled.
        hw_monitor = None
        if _topology_file and host_registry is not None and settings.host_hw_enabled:
            from hil_controller.host_hardware_monitor import HostHardwareMonitor

            hw_monitor = HostHardwareMonitor(_db_path, host_registry)
            await hw_monitor.start()

        app.state.db_path = _db_path
        app.state.event_bus = event_bus
        app.state.scheduler = scheduler
        app.state.host_registry = host_registry
        app.state.availability_reconciler = reconciler
        app.state.host_hardware_monitor = hw_monitor

        if settings.upnp_enabled:
            from hil_controller.upnp import open_port

            await open_port(settings.port, settings.port, settings.upnp_lease_seconds)

        log.info("hil-controller started, db=%s", _db_path)
        yield

        if hw_monitor is not None:
            await hw_monitor.stop()
        if reconciler is not None:
            await reconciler.stop()
        await scheduler.stop()
        if settings.upnp_enabled:
            from hil_controller.upnp import close_port

            await close_port(settings.port)
        log.info("hil-controller stopped")

    app = FastAPI(
        title="HIL Controller",
        version="0.1.0",
        lifespan=lifespan,
    )

    from fastapi.staticfiles import StaticFiles

    from hil_controller.api.aux import router as aux_router
    from hil_controller.api.cameras import router as cameras_router
    from hil_controller.api.devices import router as devices_router
    from hil_controller.api.firmware import router as firmware_router
    from hil_controller.api.health import router as health_router
    from hil_controller.api.hosts import router as hosts_router
    from hil_controller.api.jobs import router as jobs_router
    from hil_controller.api.leases import router as leases_router
    from hil_controller.api.strands import router as strands_router
    from hil_controller.api.targets import router as targets_router
    from hil_controller.api.topology import router as topology_router
    from hil_controller.web.router import router as web_router

    app.include_router(health_router)
    app.include_router(jobs_router)
    app.include_router(hosts_router)
    app.include_router(devices_router)
    app.include_router(aux_router)
    app.include_router(cameras_router)
    app.include_router(topology_router)
    app.include_router(strands_router)
    app.include_router(leases_router)
    app.include_router(targets_router)
    app.include_router(firmware_router)
    app.include_router(web_router)

    _static = Path(__file__).parent / "web" / "static"
    app.mount("/ui/static", StaticFiles(directory=str(_static)), name="ui-static")

    return app


def cli() -> None:
    import uvicorn

    from hil_controller.config import get_settings

    settings = get_settings()
    uvicorn.run(
        "hil_controller.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        reload=False,
        log_config=None,
        log_level="info",
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cli()
