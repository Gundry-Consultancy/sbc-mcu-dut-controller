"""Camera monitoring adapter package."""

from hil_controller.adapters.camera.calibration import compute_scale, transform_roi
from hil_controller.adapters.camera.capture import CameraArtifacts, CameraCapture, ROIStore
from hil_controller.adapters.camera.monitor import ROI, CameraMonitor
from hil_controller.adapters.camera.sources import CameraSource, IPCamera, V4L2Camera

__all__ = [
    "CameraMonitor",
    "ROI",
    "CameraSource",
    "IPCamera",
    "V4L2Camera",
    "CameraCapture",
    "CameraArtifacts",
    "ROIStore",
    "compute_scale",
    "transform_roi",
]
