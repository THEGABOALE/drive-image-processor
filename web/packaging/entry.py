"""Punto de entrada de DriveImageProcessor.exe (PyInstaller)."""

import sys

from web.backend.app.launcher import main

if __name__ == "__main__":
    sys.exit(main())
