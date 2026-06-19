"""Shared pytest fixtures for hil-controller tests."""

import asyncio
import os
import tempfile
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("HIL_TOPOLOGY_FILE", "")
os.environ.setdefault("HIL_STATIC_TOKEN", "test-token-for-ci")


@pytest.fixture(scope="session")
def event_loop_policy():
    return asyncio.DefaultEventLoopPolicy()


@pytest_asyncio.fixture
async def app(tmp_path):
    # Use a real temp file so all aiosqlite connections share the same DB.
    db_file = str(tmp_path / "test.db")
    os.environ["HIL_DB_PATH"] = db_file

    from hil_controller.main import create_app

    application = create_app(db_path=db_file)
    async with application.router.lifespan_context(application):
        yield application


@pytest_asyncio.fixture
async def client(app) -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def authed_client(app) -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer test-token-for-ci"},
    ) as ac:
        yield ac


@pytest.fixture
def tmp_topology(tmp_path: Path) -> Path:
    """Write a minimal topology YAML for tests that need real host config."""
    content = """
hosts:
  - id: fake-sbc-host
    role: sbc-fleet
    addr: 127.0.0.1
    transport: fake
    ssh_user: pi
    ssh_key_path: /tmp/fake-key
    max_concurrent_jobs: 1
    capabilities: []

  - id: fake-mcu-host
    role: microcontroller-fleet
    addr: 127.0.0.2
    transport: fake
    ssh_user: pi
    ssh_key_path: /tmp/fake-key
    max_concurrent_jobs: null
    capabilities: [cameras]

devices:
  - id: fake-pi5-01
    host_id: fake-sbc-host
    kind: sbc
    model: pi5
    capabilities: [linux, python-snapper]
    pool: wippersnapper-python
    status: available

  - id: fake-qtpy-01
    host_id: fake-mcu-host
    kind: microcontroller
    model: esp32-s3
    capabilities: [spi, i2c]
    pool: public
    status: available
    peripheral_ids: [fake-oled-periph-01]

auxes:
  - id: fake-oled-01
    kind: display
    model: ssd1306
    capabilities: [display, "display:128x32", "display:i2c"]
    interface: i2c
    observability: camera
    pool: public
    status: available

peripherals:
  - id: fake-oled-periph-01
    kind: display
    model: "OLED 128x32"
    product_url: "https://adafru.it/2900"
    notes: "Monochrome OLED FeatherWing 128x32"

connections:
  - aux: fake-oled-01
    device: fake-qtpy-01
"""
    p = tmp_path / "topology.yaml"
    p.write_text(content)
    return p


@pytest_asyncio.fixture
async def seeded_app(tmp_path: Path, tmp_topology: Path):
    """App with topology seeded into the DB."""
    db_file = str(tmp_path / "seeded.db")
    os.environ["HIL_DB_PATH"] = db_file

    from hil_controller.main import create_app

    application = create_app(db_path=db_file, topology_file=str(tmp_topology))
    async with application.router.lifespan_context(application):
        yield application


@pytest_asyncio.fixture
async def seeded_client(seeded_app) -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(
        transport=ASGITransport(app=seeded_app),
        base_url="http://test",
        headers={"Authorization": "Bearer test-token-for-ci"},
    ) as ac:
        yield ac
