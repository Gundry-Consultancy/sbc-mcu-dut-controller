"""Tests for adapters.camera.report — HTML generation, no cv2 needed."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from hil_controller.adapters.camera.report import generate_report


def _make_frame(n: int, ts: float, ctype: str, path: str | None = None) -> MagicMock:
    f = MagicMock()
    f.frame_number = n
    f.timestamp_s = ts
    f.change_type = ctype
    f.path = path
    return f


def test_generate_report_creates_html_file(tmp_path):
    frames = [_make_frame(0, 0.0, "initial", "/tmp/f0.jpg")]
    out = generate_report(frames, "test-job", output_dir=str(tmp_path))
    assert Path(out).exists()
    assert out.endswith(".html")


def test_generate_report_includes_test_name(tmp_path):
    out = generate_report([_make_frame(0, 0.0, "initial")], "my-test", output_dir=str(tmp_path))
    assert "my-test" in Path(out).read_text()


def test_generate_report_shows_all_change_types(tmp_path):
    frames = [
        _make_frame(0, 0.0, "initial"),
        _make_frame(1, 1.0, "display"),
        _make_frame(2, 2.0, "led"),
    ]
    content = Path(generate_report(frames, "test", output_dir=str(tmp_path))).read_text()
    assert "initial" in content.lower()
    assert "display" in content.lower()
    assert "led" in content.lower()


def test_generate_report_empty_frames_valid_html(tmp_path):
    content = Path(generate_report([], "empty", output_dir=str(tmp_path))).read_text()
    assert "<!DOCTYPE html>" in content
    assert "Total distinct frames:</strong> 0" in content


def test_generate_report_frame_count(tmp_path):
    frames = [_make_frame(i, float(i), "display") for i in range(5)]
    content = Path(generate_report(frames, "count-test", output_dir=str(tmp_path))).read_text()
    assert "5" in content


def test_generate_report_escapes_xss(tmp_path):
    content = Path(
        generate_report([], "<script>alert(1)</script>", output_dir=str(tmp_path))
    ).read_text()
    assert "<script>" not in content


def test_generate_report_omits_empty_groups(tmp_path):
    frames = [_make_frame(0, 0.0, "initial")]
    content = Path(generate_report(frames, "t", output_dir=str(tmp_path))).read_text()
    # no Display or Led section headers (no display/led frames)
    assert "<h2>Display" not in content
    assert "<h2>Led" not in content


def test_generate_report_filename_safe(tmp_path):
    out = generate_report([], "a/b::c d", output_dir=str(tmp_path))
    assert "/" not in Path(out).name
    assert "::" not in Path(out).name
