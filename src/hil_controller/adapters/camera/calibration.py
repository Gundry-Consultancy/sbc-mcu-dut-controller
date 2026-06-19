"""Scale/offset calibration: map reference-image ROIs to live camera frames."""

from __future__ import annotations

import math


def compute_scale(
    detected: dict[str, tuple[int, int]],
    reference: dict[str, tuple[int, int]],
) -> float:
    """Return scale factor (detected_pixels / reference_pixels) from matching QR centers.

    Uses all common QR pairs and averages the computed scales.
    Returns 1.0 if fewer than two common points exist.
    """
    common = [k for k in detected if k in reference]
    if len(common) < 2:
        return 1.0
    scales = []
    for i in range(len(common)):
        for j in range(i + 1, len(common)):
            ka, kb = common[i], common[j]
            det_dist = math.hypot(
                detected[kb][0] - detected[ka][0], detected[kb][1] - detected[ka][1]
            )
            ref_dist = math.hypot(
                reference[kb][0] - reference[ka][0], reference[kb][1] - reference[ka][1]
            )
            if ref_dist > 1e-6:
                scales.append(det_dist / ref_dist)
    return sum(scales) / len(scales) if scales else 1.0


def transform_roi(
    roi: tuple[int, int, int, int],
    detected: dict[str, tuple[int, int]],
    reference: dict[str, tuple[int, int]],
) -> tuple[int, int, int, int]:
    """Transform ROI (x,y,w,h) from reference coordinates to current frame coordinates.

    Computes scale from matching QR centers, then estimates the translation offset
    by averaging the displacement of all common QR positions.
    """
    scale = compute_scale(detected, reference)
    common = [k for k in detected if k in reference]
    tx, ty = 0.0, 0.0
    if common:
        offsets_x = [detected[k][0] - reference[k][0] * scale for k in common]
        offsets_y = [detected[k][1] - reference[k][1] * scale for k in common]
        tx = sum(offsets_x) / len(offsets_x)
        ty = sum(offsets_y) / len(offsets_y)
    x, y, w, h = roi
    return (int(x * scale + tx), int(y * scale + ty), int(w * scale), int(h * scale))
