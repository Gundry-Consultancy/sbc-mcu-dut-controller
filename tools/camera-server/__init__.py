"""HIL camera snapshot server.

A small HTTP server that keeps a camera pipeline warm and serves fresh
JPEG snapshots or an MJPEG stream. Pluggable backends keep it portable
across libcamera Pi CSI sensors, UVC webcams, and future stacks.
"""
