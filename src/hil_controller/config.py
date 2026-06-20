from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HIL_", env_file=".env", extra="ignore")

    db_path: str = "/var/lib/hil/jobs.db"
    topology_file: str = "/etc/hil/topology.yaml"
    host: str = "0.0.0.0"
    port: int = 8080

    # Bootstrap auth: comma-separated plaintext tokens for initial setup.
    # In production, use scripts/mint-token.py to write argon2-hashed rows instead.
    static_token: str = ""

    long_poll_max_timeout: int = 600
    long_poll_default_timeout: int = 300

    upnp_enabled: bool = False
    upnp_lease_seconds: int = 3600

    # Path to vendor/protomq/scripts/ for the web scripts browser.  Empty = disabled.
    scripts_dir: str = ""

    # Local directory for job assets (uploaded firmware, artifacts).  Defaults to
    # a jobs/ subdirectory next to the DB.
    jobs_dir: str = ""

    # WipperSnapper Arduino + protoMQ defaults for the Arduino WS test job form.
    # Override via HIL_WIPPERSNAPPER_ARDUINO_REPO / HIL_PROTOMQ_REPO / HIL_PROTOMQ_DEFAULT_REF.
    wippersnapper_arduino_repo: str = (
        "https://github.com/adafruit/Adafruit_WipperSnapper_Arduino.git"
    )
    protomq_repo: str = "https://github.com/adafruit/protomq.git"
    protomq_default_ref: str = "main"

    # PlatformIO defaults for the Arduino WS test job form.
    # Override via HIL_PIO_DEFAULT_ENV / HIL_SERIAL_DEFAULT_PORT.
    pio_default_env: str = "adafruit_feather_esp32s3"
    serial_default_port: str = "/dev/ttyACM0"

    # Default MQTT broker host for the Arduino WS test job form.
    # Override via HIL_MQTT_DEFAULT_HOST.
    mqtt_default_host: str = "127.0.0.1"

    # LAN IP the DUT uses to reach the controller when protomq/build run on the
    # controller (per-phase execution-location). Override via HIL_CONTROLLER_IP.
    controller_ip: str = "192.168.1.169"

    # firmware-bench protomq: the broker branch + its V2 protobuf source repo/ref
    # (cloned as the sibling ../Wippersnapper_Protobuf). V1 protos are bundled in
    # the protomq branch. Override via HIL_FIRMWARE_BENCH_PROTOMQ_REF etc.
    firmware_bench_protomq_ref: str = "displays-v2-testing"
    protobuf_repo: str = "https://github.com/adafruit/Wippersnapper_Protobuf.git"
    protobuf_ref: str = "api-v2"

    # Device availability self-rectification (see docs/device-availability.md).
    # Override via HIL_AVAIL_RETRY_ATTEMPTS / HIL_AVAIL_RETRY_WINDOW_S /
    # HIL_AVAIL_RECONCILE_S.
    avail_retry_attempts: int = 3
    avail_retry_window_s: int = 180
    avail_reconcile_s: int = 30
    # Auto-reboot a DUT host when its USB stack wedges (dwc_otg, not
    # runtime-rebindable). OFF by default — rebooting a shared bench host is
    # disruptive; when off the controller flags the host reboot_required and logs
    # that a manual reboot is needed. Override via HIL_AUTO_HOST_REBOOT.
    auto_host_reboot: bool = False
    # Expected DUT-host downtime advertised to CI callers when a wedge/auto-reboot
    # is triggered: the flagged devices get retry_after = now + this many seconds
    # (and the reason notes "back ~Ns"), so a `wait_for_target_available` CI helper
    # sleeps until then and re-polls rather than hard-failing. A dwc_otg reboot
    # self-recovers in ~3–5 min. Override via HIL_HOST_REBOOT_ETA_S.
    host_reboot_eta_s: int = 300

    # Host hardware auto-detection (see host_hardware.py). The monitor refreshes
    # live load every host_load_s and re-probes static specs once they're older
    # than host_specs_refresh_s. The speed benchmark is manual-only (it loads the
    # box). speed_baseline_* are the idle-Pi-Zero-W denominators (=1.0×); the
    # openssl figure is measured, the sysbench one is a placeholder to calibrate.
    # Override via HIL_HOST_HW_ENABLED / HIL_HOST_LOAD_S / HIL_HOST_SPECS_REFRESH_S
    # / HIL_SPEED_BASELINE_OPENSSL / HIL_SPEED_BASELINE_SYSBENCH.
    host_hw_enabled: bool = True
    host_load_s: int = 60
    host_specs_refresh_s: int = 86400
    speed_baseline_openssl: float = 29800.0
    speed_baseline_sysbench: float = 50.0

    # Bench secrets for the version-bisection UI (so the operator never re-enters
    # them in the form). The DUT joins this WiFi to reach the controller's
    # per-session protomq broker; the IO creds are placeholders (protomq
    # autoresponds). VALUES live in run/controller.env, NEVER in the repo. Override
    # via HIL_BENCH_WIFI_SSID / HIL_BENCH_WIFI_PASSWORD / HIL_BENCH_IO_USERNAME /
    # HIL_BENCH_IO_KEY.
    bench_wifi_ssid: str = ""
    bench_wifi_password: str = ""
    bench_io_username: str = "hil"
    bench_io_key: str = "placeholder"

    # git credential helper for per-session clones (protomq + protobuf source).
    # Empty by default; non-admins pass a per-repo PAT instead. The bench sets
    # HIL_GIT_CREDENTIAL_HELPER='!sudo gh auth git-credential' in controller.env.
    git_credential_helper: str = ""


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings  # noqa: PLW0603 - module-level settings singleton
    if _settings is None:
        _settings = Settings()
    return _settings


def resolve_jobs_dir() -> str:
    """Local directory for job assets (uploaded firmware, captured logs).

    ``HIL_JOBS_DIR`` when set, else a ``jobs/`` subdirectory next to the DB.
    Shared by the web router and the queue worker so both agree on the path.
    """
    cfg = get_settings()
    if cfg.jobs_dir:
        return cfg.jobs_dir
    db = cfg.db_path
    return str(Path(db).parent / "jobs") if db else "/tmp/hil-jobs"
