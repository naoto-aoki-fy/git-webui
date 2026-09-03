"""Executable entry point for ``python backend/app.py``."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if __name__ == "__main__":
    from backend.git_webui.cli import main

    main()
