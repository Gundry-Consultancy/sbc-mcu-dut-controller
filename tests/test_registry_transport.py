"""_build_transport must pick LocalTransport / SSH / none from the `transport` field."""

from __future__ import annotations

import pytest

from hil_controller.hosts.local import LocalTransport
from hil_controller.hosts.registry import RealHostRegistry
from hil_controller.hosts.ssh import SSHTransport


def _reg() -> RealHostRegistry:
    return RealHostRegistry(topology_file="", db_path=":memory:")


def test_transport_local_yields_local_transport():
    t = _reg()._build_transport({"id": "localhost", "transport": "local", "addr": "localhost"})
    assert isinstance(t, LocalTransport)


def test_transport_ssh_yields_ssh_transport():
    t = _reg()._build_transport({"id": "rpi-hil002", "transport": "ssh", "addr": "rpi-hil002"})
    assert isinstance(t, SSHTransport)


def test_transport_none_raises():
    with pytest.raises(ValueError):
        _reg()._build_transport({"id": "tachyon-protomq", "transport": "none", "addr": ""})


def test_legacy_kind_local_still_works():
    t = _reg()._build_transport({"id": "x", "kind": "local", "addr": "localhost"})
    assert isinstance(t, LocalTransport)


def test_missing_transport_defaults_to_ssh():
    t = _reg()._build_transport({"id": "x", "addr": "somehost"})
    assert isinstance(t, SSHTransport)
