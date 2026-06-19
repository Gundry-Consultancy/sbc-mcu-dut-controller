"""Tests for adapters.camera.calibration — pure math, no cv2 needed."""

from __future__ import annotations

import math

import pytest

from hil_controller.adapters.camera.calibration import compute_scale, transform_roi


def test_compute_scale_two_common_horizontal():
    detected = {"a": (200, 100), "b": (400, 100)}  # dist = 200
    reference = {"a": (100, 50), "b": (200, 50)}  # dist = 100
    assert compute_scale(detected, reference) == pytest.approx(2.0)


def test_compute_scale_three_common_all_2x():
    detected = {"a": (0, 0), "b": (200, 0), "c": (0, 200)}
    reference = {"a": (0, 0), "b": (100, 0), "c": (0, 100)}
    assert compute_scale(detected, reference) == pytest.approx(2.0)


def test_compute_scale_one_common_returns_1():
    detected = {"a": (200, 100)}
    reference = {"a": (100, 50)}
    assert compute_scale(detected, reference) == 1.0


def test_compute_scale_no_common_returns_1():
    assert compute_scale({"x": (1, 2)}, {"y": (3, 4)}) == 1.0


def test_compute_scale_empty_returns_1():
    assert compute_scale({}, {}) == 1.0


def test_compute_scale_same_positions_returns_1():
    detected = {"a": (100, 100), "b": (200, 200)}
    reference = {"a": (100, 100), "b": (200, 200)}
    assert compute_scale(detected, reference) == pytest.approx(1.0)


def test_compute_scale_diagonal_pair():
    detected = {"a": (0, 0), "b": (300, 400)}  # dist=500
    reference = {"a": (0, 0), "b": (150, 200)}  # dist=250
    assert compute_scale(detected, reference) == pytest.approx(2.0)


def test_transform_roi_identity_scale_no_offset():
    roi = (10, 20, 100, 50)
    detected = {"a": (100, 100), "b": (200, 100)}
    reference = {"a": (100, 100), "b": (200, 100)}
    result = transform_roi(roi, detected, reference)
    assert result == (10, 20, 100, 50)


def test_transform_roi_2x_scale_no_offset():
    roi = (50, 50, 100, 80)
    detected = {"a": (0, 0), "b": (200, 0)}
    reference = {"a": (0, 0), "b": (100, 0)}
    x, y, w, h = transform_roi(roi, detected, reference)
    assert w == 200
    assert h == 160


def test_transform_roi_with_pure_translation():
    # scale=1, all QRs shifted by (+50, +30)
    detected = {"a": (150, 130), "b": (250, 130)}
    reference = {"a": (100, 100), "b": (200, 100)}
    roi = (100, 100, 80, 60)
    x, y, w, h = transform_roi(roi, detected, reference)
    assert x == 150
    assert y == 130
    assert w == 80
    assert h == 60


def test_transform_roi_no_common_returns_same():
    roi = (10, 20, 100, 50)
    result = transform_roi(roi, {}, {})
    assert result == (10, 20, 100, 50)


def test_transform_roi_scale_and_translation():
    # 2x scale + offset of (+10, +5)
    detected = {"a": (10, 5), "b": (210, 5)}  # 2x + tx=10, ty=5
    reference = {"a": (0, 0), "b": (100, 0)}
    roi = (0, 0, 50, 40)
    x, y, w, h = transform_roi(roi, detected, reference)
    assert w == 100
    assert h == 80
    assert x == 10  # 0*2 + 10
    assert y == 5  # 0*2 + 5
