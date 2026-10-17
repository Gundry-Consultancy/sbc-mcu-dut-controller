#!/usr/bin/env python3
"""Read hardware identity observations; see --help. Install the controller package first."""

from hil_controller.twin_collectors import main

if __name__ == "__main__":
    raise SystemExit(main())
