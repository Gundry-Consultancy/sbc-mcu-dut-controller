"""scripts/solenoid_hub_cli.py — venv re-exec detection."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

CLI = Path(__file__).resolve().parents[1] / "scripts" / "solenoid_hub_cli.py"


@pytest.fixture
def cli():
    spec = importlib.util.spec_from_file_location("solenoid_hub_cli", CLI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_venv(tmp_path: Path) -> Path:
    py = tmp_path / "venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    try:
        # A real venv's bin/python is a symlink to the system interpreter.
        os.symlink(sys.executable, py)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    return py


def test_system_python_is_not_in_venv_even_though_symlink_resolves_same(cli, tmp_path, monkeypatch):
    venv_py = _fake_venv(tmp_path)
    # Regression: realpath(venv python) == realpath(sys.executable), which used to
    # count as "already in the venv" and skipped the re-exec, so Blinka never loaded.
    assert os.path.realpath(venv_py) == os.path.realpath(sys.executable)
    monkeypatch.setattr(sys, "prefix", "/usr")
    assert cli._in_venv(str(venv_py)) is False


def test_running_inside_the_venv_is_detected(cli, tmp_path, monkeypatch):
    venv_py = _fake_venv(tmp_path)
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    assert cli._in_venv(str(venv_py)) is True


def test_reexec_happens_from_system_python(cli, tmp_path, monkeypatch):
    venv_py = _fake_venv(tmp_path)
    monkeypatch.setattr(sys, "prefix", "/usr")
    monkeypatch.setenv("HIL_SOLENOID_VENV", str(venv_py))
    monkeypatch.delenv("_HIL_SOLENOID_VENV_REEXEC", raising=False)
    calls = []
    monkeypatch.setattr(os, "execv", lambda path, argv: calls.append((path, argv)))
    cli._maybe_reexec_in_venv()
    assert calls and calls[0][0] == str(venv_py)


def test_no_reexec_when_already_in_venv_or_guarded(cli, tmp_path, monkeypatch):
    venv_py = _fake_venv(tmp_path)
    monkeypatch.setenv("HIL_SOLENOID_VENV", str(venv_py))
    calls = []
    monkeypatch.setattr(os, "execv", lambda path, argv: calls.append((path, argv)))
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.delenv("_HIL_SOLENOID_VENV_REEXEC", raising=False)
    cli._maybe_reexec_in_venv()
    monkeypatch.setattr(sys, "prefix", "/usr")
    monkeypatch.setenv("_HIL_SOLENOID_VENV_REEXEC", "1")
    cli._maybe_reexec_in_venv()
    assert calls == []
