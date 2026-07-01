"""Host registry: loads topology YAML, provides adapters to the scheduler."""

from __future__ import annotations

import json
import logging
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

log = logging.getLogger(__name__)


class HostRegistry:
    #: target.requires[].kind values that denote an I2C-strand prerequisite.
    STRAND_REQUIRE_KINDS = frozenset({"i2c_strand", "strand", "component"})

    def __init__(self, topology_file: str) -> None:
        self.topology_file = topology_file
        self._hosts: list[dict[str, Any]] = []
        self._devices: list[dict[str, Any]] = []
        self._semaphores: dict[str, Any] = {}
        #: device_id -> list of {strand_id, caps:set, channel} routes it can receive.
        self._device_strand_routes: dict[str, list[dict[str, Any]]] = {}

    def load(self) -> None:
        if not self.topology_file:
            return
        path = Path(self.topology_file)
        if not path.exists():
            log.warning("Topology file not found: %s", path)
            return
        data = yaml.safe_load(path.read_text())
        self._hosts = data.get("hosts", [])
        self._devices = data.get("devices", [])
        self._index_strands(data.get("strands", []))
        import asyncio

        for h in self._hosts:
            max_jobs = h.get("max_concurrent_jobs", 1)
            if max_jobs is not None:
                self._semaphores[h["id"]] = asyncio.Semaphore(max_jobs)
            else:
                self._semaphores[h["id"]] = None  # unbounded

        log.info("Loaded %d hosts, %d devices from %s", len(self._hosts), len(self._devices), path)

    def _index_strands(self, strands: list[dict[str, Any]]) -> None:
        """Build device_id -> routed-strand capability index from topology strands.

        A strand's capabilities are the union of its components' ``capabilities``.
        Each ``routes`` entry maps a device to the analog-mux channel that
        connects the strand to it.
        """
        self._device_strand_routes = {}
        for s in strands:
            caps: set[str] = set()
            for c in s.get("components", []):
                # Match on both the declared capability tags AND the component's
                # model short-name (e.g. "pmsa003i", "scd30"), case-insensitively.
                caps.update((cap or "").strip().lower() for cap in (c.get("capabilities") or []))
                model = (c.get("model") or "").strip().lower()
                if model:
                    caps.add(model)
            for r in s.get("routes", []):
                dev_id = r.get("device")
                if not dev_id:
                    continue
                self._device_strand_routes.setdefault(dev_id, []).append(
                    {"strand_id": s["id"], "caps": caps, "channel": r.get("channel")}
                )

    @staticmethod
    def _required_strand_caps(target: dict[str, Any]) -> set[str]:
        """Union of capabilities from target.requires entries that name a strand."""
        want: set[str] = set()
        for req in target.get("requires") or []:
            if (req.get("kind") or "") in HostRegistry.STRAND_REQUIRE_KINDS:
                want.update((cap or "").strip().lower() for cap in (req.get("capabilities") or []))
        return want

    def strand_for_device(self, device_id: str, caps: set[str]) -> dict[str, Any] | None:
        """First strand routed to ``device_id`` whose capabilities cover ``caps``."""
        for route in self._device_strand_routes.get(device_id, []):
            if caps.issubset(route["caps"]):
                return route
        return None

    def find_device_for_job(self, request: dict[str, Any]) -> tuple[dict, dict] | None:
        """Return (host, device) for the given job request, or None if no seat.

        An explicit ``device.id`` selector is authoritative: it matches that
        device by id alone (still requiring it to be available and owned by a
        known host), bypassing the pool/kind/model/capability gates. Operators
        who pick a specific device in the UI get that device regardless of which
        pool a job-builder happens to pin.
        """
        target = request.get("target", {})
        device_sel = target.get("device", {})
        pool = target.get("pool", "public")
        want_id = device_sel.get("id")
        want_caps = set(device_sel.get("capabilities") or [])
        want_strand_caps = self._required_strand_caps(target)

        for device in self._devices:
            if device.get("status", "available") != "available":
                continue
            host = next((h for h in self._hosts if h["id"] == device["host_id"]), None)
            if host is None:
                continue

            if want_id:
                if device["id"] == want_id:
                    return host, device
                continue

            if pool and device.get("pool") != pool:
                continue
            if device_sel.get("kind") and device["kind"] != device_sel["kind"]:
                continue
            if device_sel.get("model") and device["model"] != device_sel["model"]:
                continue
            if want_caps and not want_caps.issubset(set(device.get("capabilities") or [])):
                continue
            # I2C-strand prerequisite: the device must be able to receive a strand
            # that provides every required strand capability (one strand is muxed
            # onto the DUT at a time, so a single strand must cover them all).
            if want_strand_caps and self.strand_for_device(device["id"], want_strand_caps) is None:
                continue
            return host, device

        return None

    def _no_match_reason(self, request: dict[str, Any]) -> str:
        target = request.get("target", {})
        device_sel = target.get("device", {})
        candidates = (
            ", ".join(
                f"{d['id']}(pool={d.get('pool')},kind={d.get('kind')},status={d.get('status', 'available')})"  # noqa: E501
                for d in self._devices
            )
            or "<none>"
        )
        return (
            "No available device matched job target "
            f"(pool={target.get('pool', 'public')!r}, id={device_sel.get('id')!r}, "
            f"kind={device_sel.get('kind')!r}, capabilities={device_sel.get('capabilities') or []}). "  # noqa: E501
            f"Candidates: {candidates}"
        )

    async def get_adapter(self, job_id: str) -> Any:
        from hil_controller.db.connection import get_db, update_job_state

        # We need the job's request to resolve a device
        # Import app state via the global scheduler (passed at construction)
        # For now, return a no-op adapter; the scheduler wires up the db_path
        # and this method is overridden by _RegistryAdapter below
        from hil_controller.queue.scheduler import _FakeAdapter

        return _FakeAdapter()


class RealHostRegistry(HostRegistry):
    """Registry that returns real SSH-backed adapters."""

    def __init__(self, topology_file: str, db_path: str) -> None:
        super().__init__(topology_file)
        self.db_path = db_path

    async def get_adapter(self, job_id: str) -> Any:
        import json

        from hil_controller.adapters.git_deploy import GitDeployAdapter
        from hil_controller.db.connection import get_db, get_job, update_job_state
        from hil_controller.hosts.ssh import SSHTransport

        async with get_db(self.db_path) as db:
            row = await get_job(db, job_id)
        if row is None:
            from hil_controller.queue.scheduler import _FakeAdapter

            return _FakeAdapter()

        request = json.loads(row["request_json"])
        result = self.find_device_for_job(request)
        if result is None:
            reason = self._no_match_reason(request)
            log.warning("No matching device for job %s: %s", job_id, reason)
            return _UnmatchedAdapter(reason)

        host, device = result

        # The topology entry is only a seed; the live DB holds the authoritative
        # runtime hardware-addressing fields (serial_port, hub/solenoid wiring,
        # flasher, build_target) that an operator/inventory may have set after
        # seeding. Overlay them so the adapter can actually reach the bench —
        # without this, a device whose ports live only in the DB (not the
        # topology file) flashes to an empty port.
        async with get_db(self.db_path) as db:
            cur = await db.execute(
                "SELECT status, unavailable_reason, serial_port, hub_host_id, "
                "hub_port_path, solenoid_channel, flasher, usb_serial, build_target "
                "FROM devices WHERE id=?",
                (device["id"],),
            )
            drow = await cur.fetchone()
            if drow is not None:
                d = dict(drow)
                # Gate: the DB is authoritative for runtime availability. Refuse
                # to start a job on a device the DB has flagged unavailable (e.g.
                # a wedged host pending reboot) — the topology matcher only knows
                # the seed status. Fail fast with the reason rather than flashing
                # a device that isn't there.
                if d.get("status") == "unavailable":
                    reason = d.get("unavailable_reason") or "device unavailable"
                    log.warning(
                        "job %s rejected: device %s unavailable (%s)", job_id, device["id"], reason
                    )
                    return _UnmatchedAdapter(f"device {device['id']} unavailable: {reason}")
                device = {**device, **{k: v for k, v in d.items() if v is not None}}
            await update_job_state(
                db,
                job_id,
                "assigned",
                assigned_host=host["id"],
                assigned_device=device["id"],
            )

        return self.make_adapter(host, device, request, job_id)

    def _build_transport(self, host: dict[str, Any]) -> Any:
        from hil_controller.hosts.ssh import SSHTransport

        # Host records carry a ``transport`` field (ssh | local | none); the older
        # ``kind == 'local'`` form is kept as a fallback for any legacy caller.
        transport = host.get("transport") or ("local" if host.get("kind") == "local" else "ssh")
        if transport == "local":
            from hil_controller.hosts.local import LocalTransport

            return LocalTransport()
        if transport == "none":
            raise ValueError(f"host {host.get('id')!r} has transport=none (no exec transport)")
        return SSHTransport(
            host=host["addr"],
            user=host.get("ssh_user", "pi"),
            key_path=Path(host["ssh_key_path"]) if host.get("ssh_key_path") else None,
            known_hosts=host.get("known_hosts"),
        )

    def transport_for(self, host_id: str) -> Any:
        """Public lookup: return a HostTransport for the named host.

        Raises ``KeyError`` if ``host_id`` is not in the loaded topology.
        Used by API endpoints that need to invoke commands on a bench
        host outside the job-scheduling flow (e.g. ``/v1/hosts/{id}/
        usbip/exportable``, the M3.5 leases UI's hub-status panel).
        """
        for host in self._hosts:
            if host["id"] == host_id:
                return self._build_transport(host)
        raise KeyError(f"unknown host_id: {host_id}")

    def make_adapter(
        self, host: dict[str, Any], device: dict[str, Any], request: dict[str, Any], job_id: str
    ) -> Any:
        """Construct the adapter for a matched (host, device). No DB access.

        Routing:
          * arduino-ws jobs (``params.exec`` present, git-source) get the
            phase-aware :class:`ArduinoWsExecAdapter`. Its **controller**
            transport is always ``LocalTransport`` (the host running
            hil-controller — that is what "build/flash on the controller"
            means), and its **dut-host** transport is the USB-server host
            (``hub_host_id``, defaulting to the device's ``host_id``).
          * other git-source jobs get :class:`GitDeployAdapter` (single host).
          * non-source jobs get :class:`ShellScriptAdapter`.
        """
        from hil_controller.adapters.git_deploy import GitDeployAdapter

        transport = self._build_transport(host)

        payload = request.get("payload") or {}
        params = request.get("params") or {}
        source = payload.get("source", {})
        secrets = request.get("secrets", {})
        secrets_format = params.get("secrets_format", "env")

        if request.get("script") == "firmware-bench":
            from hil_controller.adapters.firmware_bench import FirmwareBenchAdapter
            from hil_controller.config import get_settings, resolve_jobs_dir
            from hil_controller.hosts.local import LocalTransport

            # Flash/serial/MSC act on the DUT's USB-server host; protomq runs on
            # the controller (LocalTransport). Solenoid power-control lives on the
            # same hub host as the DUT.
            hub_host_id = device.get("hub_host_id") or host["id"]
            dut_host = next((h for h in self._hosts if h["id"] == hub_host_id), host)
            dut_transport = self._build_transport(dut_host)
            cfg = get_settings()
            # If the job requires an I2C strand and this device is routed to one
            # that provides it, hand the strand id to the adapter so it auto-mux's
            # the strand onto the DUT (a select_i2c_strand stage) before flashing.
            strand_caps = self._required_strand_caps(request.get("target", {}))
            route = self.strand_for_device(device["id"], strand_caps) if strand_caps else None
            return FirmwareBenchAdapter(
                controller_transport=LocalTransport(),
                dut_transport=dut_transport,
                hub_transport=dut_transport,
                job_id=job_id,
                device=device,
                params=params,
                payload=payload,
                secrets=secrets,
                controller_ip=cfg.controller_ip,
                protomq_repo=params.get("protomq_repo", ""),
                protomq_ref=params.get("protomq_ref", ""),
                jobs_dir=resolve_jobs_dir(),
                auto_strand_id=(route["strand_id"] if route else None),
            )

        if not source:
            from hil_controller.adapters.shell_script import ShellScriptAdapter

            return ShellScriptAdapter(
                transport=transport,
                script=request.get("script", "true"),
            )

        exec_plan = params.get("exec")
        if exec_plan:
            from hil_controller.adapters.arduino_ws_exec import ArduinoWsExecAdapter
            from hil_controller.hosts.local import LocalTransport

            # "controller" == the box running hil-controller == LocalTransport,
            # regardless of where the DUT's USB physically lives. The DUT's
            # USB-server host (hub_host_id, default the device's host_id) is the
            # "dut-host" transport and the usbip attach target.
            hub_host_id = device.get("hub_host_id") or host["id"]
            dut_host = next((h for h in self._hosts if h["id"] == hub_host_id), host)
            dut_transport = self._build_transport(dut_host)

            return ArduinoWsExecAdapter(
                controller_transport=LocalTransport(),
                dut_transport=dut_transport,
                job_id=job_id,
                source=source,
                params=params,
                exec_plan=exec_plan,
                device=device,
                server_addr=dut_host.get("addr", ""),
                secrets=secrets,
                secrets_format=secrets_format,
            )

        return GitDeployAdapter(
            transport=transport,
            job_id=job_id,
            source=source,
            params=params,
            secrets=secrets,
            secrets_format=secrets_format,
        )


class _UnmatchedAdapter:
    """Adapter returned when no device matches a job.

    Raising in ``acquire()`` routes through the worker's error path, which
    emits ``state=error`` plus a ``log`` event carrying the reason — so the
    failure is visible in the job log instead of silently passing on a fake
    adapter.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def acquire(self) -> None:
        raise RuntimeError(self.reason)

    async def reset(self) -> None:
        pass

    async def flash(self, artifact: dict) -> None:
        pass

    async def open_serial(self):
        return iter([])

    async def release(self) -> None:
        pass
