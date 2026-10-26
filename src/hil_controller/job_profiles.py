"""Portable agent job recipes. No operator credentials are read by this module."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

WIFI_HELP = (
    "Public bench Wi-Fi: SSID free4all, password password. Omit secrets for test-only protoMQ."
)
PUBLIC_SECRETS = {
    "WIFI_SSID": "free4all",
    "WIFI_PASSWORD": "password",
    "IO_USERNAME": "hil",
    "IO_KEY": "hil",
}
Runtime = Literal["wippersnapper-arduino", "wippersnapper-python", "circuitpython"]
Broker = Literal["protomq", "production", "staging", "dev-lan"]
#: Stages that call the real Adafruit IO REST API / MQTT with the job's IO_KEY.
IO_STACK_STAGES = frozenset({"io_api_component", "io_ws_publish", "io_ws_expect"})
#: Board-rescue stages: local bench hardware ops (serial poke, BOOTSEL press,
#: picotool/esptool rescue) — broker-independent, so no broker gating applies.
#: Pair them with ``params.bare: true`` so a rescue job never launches protoMQ.
RESCUE_STAGES = frozenset({"serial_probe", "enter_bootsel", "picotool_rescue", "esptool_rescue"})
IO_STACK_BROKERS = frozenset({"staging", "dev-lan"})
_IO_STAGE_RESERVED = (
    "io_host", "io_port", "io_url", "io_username", "io_key", "username", "key",
    "password", "secrets",
)


class AgentJob(BaseModel):
    """Request a HIL job; use job_guide before preparing a recipe."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    runtime: Runtime
    device_id: str = Field(
        min_length=1, description="Discover with list_targets; never invent IDs."
    )
    pool: str = "public"
    broker: Broker = "protomq"
    broker_host: str | None = None
    broker_port: int | None = Field(default=None, ge=1, le=65535)
    secrets: dict[str, SecretStr] = Field(default_factory=dict, description=WIFI_HELP)
    firmware: dict[str, Any] | None = None
    source: dict[str, Any] | None = Field(
        default=None, description="Python git source: repo, ref, optional setup/submodules."
    )
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Current controller params; stages override recipes. Read live skills first.",
    )
    files: dict[str, str] = Field(
        default_factory=dict,
        description="CircuitPython UTF-8 files, including code.py and lib/*.py.",
    )
    expect_serial: str | None = Field(
        default=None,
        min_length=1,
        description="Literal serial success marker; required for cloud MCU tests.",
    )
    requires: list[dict[str, Any]] = Field(default_factory=list)
    total_s: int = Field(default=1200, ge=30, le=86400)


def job_guide() -> dict[str, Any]:
    """Public credential guidance, shared by HTTP and MCP discovery."""
    return {
        "wifi": {"ssid": "free4all", "password": "password", "help": WIFI_HELP},
        "broker_modes": {
            "protomq": {"credentials": "test only: hil / hil", "host": "allocated per job"},
            "production": {"host": "io.adafruit.com", "port": 8883},
            "staging": {"host": "io.adafruit.us", "port": 8883},
            "dev-lan": {"host": "explicit broker_host", "port": "explicit broker_port"},
        },
        "secrets": {
            "optional": ["WIFI_SSID", "WIFI_PASSWORD"],
            "required_for_external_broker": ["IO_USERNAME", "IO_KEY"],
            "help": WIFI_HELP,
            "handling": "Supply real values only in secrets, never code, params, or URLs.",
        },
        "runtimes": ["wippersnapper-arduino", "wippersnapper-python", "circuitpython"],
        "workflow": [
            "list_targets",
            "read_skill",
            "prepare_job",
            "submit_job",
            "wait_job",
            "list_assets",
        ],
        "notes": [
            "No personal Wi-Fi/IO credentials needed; controller authentication is still required.",
            "Python jobs require source repo/ref and explicit params.entry/args for a real test.",
            "Firmware must match the board; non-ESP32 images need custom flash stages.",
            "CircuitPython needs a writable CIRCUITPY volume; boot.py must enable host writes.",
            "External brokers need firmware/test support for TLS and the chosen host/port.",
            "verify_checkin/inject_* inspect local protoMQ only; use other assertions for cloud.",
            "staging/dev-lan only: io_api_component (REST feeds), io_ws_publish (ws-w2b JSON)"
            " and io_ws_expect (ws-b2w/ws-r2b assert, guarded step) drive the real IO stack.",
            "Inspect result, verdicts, and assets. Finished or skipped tests do not prove success.",
            "Board rescue: serial_probe / enter_bootsel / picotool_rescue / esptool_rescue stages"
            " recover a wedged DUT; set params.bare=true so the job skips protoMQ entirely."
            " Probe (non-destructive) before BOOTSEL/download entry before erase/reflash.",
        ],
    }


def build_job(spec: AgentJob) -> dict[str, Any]:
    """Build the controller request, with routing isolated from secret defaults."""
    from hil_controller.adapters.bench_stages import (
        DEFAULT_FLASH_STAGES,
        validate_stages,
        walk_stages,
    )
    from hil_controller.api.jobs import JobRequest

    params = deepcopy(spec.params)
    credentials = {**PUBLIC_SECRETS, **{k: v.get_secret_value() for k, v in spec.secrets.items()}}
    unknown = set(credentials) - set(PUBLIC_SECRETS)
    if unknown:
        raise ValueError("secrets accepts only WIFI_SSID, WIFI_PASSWORD, IO_USERNAME, IO_KEY")
    external = spec.broker != "protomq"
    if not external and any(credentials[k] != PUBLIC_SECRETS[k] for k in ("IO_USERNAME", "IO_KEY")):
        raise ValueError(
            "protoMQ uses test IO credentials only; choose an external broker for live IO"
        )
    host = {"production": "io.adafruit.com", "staging": "io.adafruit.us"}.get(spec.broker)
    if external:
        if any(
            not credentials[k].strip() or credentials[k] in {"hil", "changeme", "placeholder"}
            for k in ("IO_USERNAME", "IO_KEY")
        ):
            raise ValueError(
                "External brokers require supplied IO_USERNAME and IO_KEY. " + WIFI_HELP
            )
        if spec.broker == "dev-lan":
            host = spec.broker_host
            if not host or not spec.broker_port:
                raise ValueError("dev-lan requires broker_host and broker_port")
        elif spec.broker_host:
            raise ValueError("broker_host is only valid for dev-lan")
        if not host or any(c in host for c in "/@?# \r\n"):
            raise ValueError(
                "broker_host must be a hostname or IP, without a URL scheme or credentials"
            )
    elif spec.broker_host or spec.broker_port:
        raise ValueError("protoMQ routing is allocated by the controller")
    port = spec.broker_port or 8883
    # Recipe owns routing fields; params cannot reverse broker selection.
    reserved = {"exec", "secrets", "secrets_format", "protomq", "launch_protomq"}
    if reserved.intersection(params):
        raise ValueError(
            "params cannot override recipe secrets, execution adapter, or broker routing"
        )
    env = params.get("extra_env", {})
    if any(k.startswith(("IO_", "MQTT_", "PROTOMQ_", "WIFI_", "ADAFRUIT_IO_")) for k in env):
        raise ValueError(
            "Use secrets/broker fields instead of extra_env for credentials or routing"
        )
    request: dict[str, Any] = {
        "target": {"device": {"id": spec.device_id}, "pool": spec.pool, "requires": spec.requires},
        "script": "firmware-bench",
        "params": params,
        "secrets": credentials,
        "secrets_profile": {
            "protomq": "bench-protomq",
            "production": "adafruit-production",
            "staging": "adafruit-staging",
            "dev-lan": "dev-lan",
        }[spec.broker],
        "timeouts": {"total_s": spec.total_s, "run_s": spec.total_s},
        "metadata": {"agent_runtime": spec.runtime, "broker_mode": spec.broker},
    }
    if spec.runtime == "wippersnapper-python":
        if not spec.source or not spec.source.get("repo") or not spec.source.get("ref"):
            raise ValueError("Python jobs need source.repo and source.ref")
        if not params.get("entry") or not params.get("args"):
            raise ValueError(
                "Python jobs need explicit params.entry and params.args for a real test"
            )
        if spec.files or spec.firmware:
            raise ValueError("Python SBC jobs use source, not firmware/files")
        request["script"] = "pytest-suite"
        request["payload"] = {"kind": "git-source", "source": deepcopy(spec.source)}
        params["secrets_format"] = "env"
        if external:
            # The caller's test must consume these env vars (no local-broker fixture).
            params["extra_env"] = {
                **env,
                "MQTT_HOST": host,
                "MQTT_PORT": str(port),
                "IO_URL": host,
                "IO_PORT": str(port),
            }
        else:
            params["protomq"] = {"launch_on": "controller"}
    else:
        if spec.source:
            raise ValueError("MCU jobs use firmware/files, not a Python source repo")
        params.setdefault("window_minutes", 0)
        if spec.firmware:
            params["firmware"] = deepcopy(spec.firmware)
        stages = params.get("stages")
        if stages is None:
            stages = deepcopy(DEFAULT_FLASH_STAGES) if spec.firmware else []
            if spec.runtime == "wippersnapper-arduino":
                if not stages:
                    stages.append({"type": "power_cycle"})
                stages.extend([{"type": "write_secrets_msc"}, {"type": "power_cycle"}])
                if not external:
                    stages.append({"type": "verify_checkin"})
            else:
                if not stages:
                    stages.append({"type": "power_cycle"})
                if not spec.files or "code.py" not in spec.files:
                    raise ValueError("CircuitPython jobs require files including code.py")
                stages.extend(
                    [
                        {"type": "start_serial_log"},
                        {"type": "write_circuitpython"},
                        {"type": "power_cycle"},
                    ]
                )
            if spec.expect_serial:
                last_reset = max(i for i, s in enumerate(stages) if s["type"] == "power_cycle")
                stages.insert(last_reset, {"type": "serial_checkpoint"})
                stages.append({"type": "assert_serial", "contains": spec.expect_serial})
            elif external or spec.runtime == "circuitpython":
                raise ValueError("Set expect_serial to a success marker emitted by this test")
            params["stages"] = stages
        if not isinstance(stages, list) or not stages:
            raise ValueError("stages must be a nonempty list")
        validate_stages(stages)
        for stage in walk_stages(stages):
            kind = stage["type"]
            if external and (
                kind in {"launch_protomq", "verify_checkin"} or kind.startswith("inject_")
            ):
                raise ValueError(
                    "External-broker recipes cannot use local protoMQ assertion/injection stages"
                )
            if kind in IO_STACK_STAGES:
                if spec.broker not in IO_STACK_BROKERS:
                    raise ValueError(
                        f"{kind} drives the real IO stack and is allowed only for "
                        "broker=staging or dev-lan (never production or protoMQ)"
                    )
                if any(k in stage for k in _IO_STAGE_RESERVED):
                    raise ValueError(
                        "IO host and credentials come from the job broker/secrets, not stages"
                    )
                if spec.broker != "dev-lan" and ("io_api_base" in stage or "tls" in stage):
                    raise ValueError("io_api_base / tls overrides are only for dev-lan brokers")
                stage.update(io_host=host, io_port=port)
            if kind in {"write_secrets_msc", "write_circuitpython"}:
                if any(k in stage for k in ("io_url", "io_port", "settings", "secrets")):
                    raise ValueError("Set broker/secrets on the job, not on individual stages")
                if external:
                    stage.update(io_url=host, io_port=port)
                if kind == "write_circuitpython":
                    from hil_controller.adapters.circuitpython import validate_files

                    validate_files(spec.files)
                    stage["files"] = deepcopy(spec.files)
        if spec.runtime == "circuitpython" and not external:
            params["launch_protomq"] = True
            if not any(s["type"] == "launch_protomq" for s in stages):
                stages.insert(0, {"type": "launch_protomq"})
    return JobRequest.model_validate(request).model_dump()
