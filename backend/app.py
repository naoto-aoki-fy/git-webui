"""Compatibility entry point for imports and ``python backend/app.py``."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.git_webui import application as _application
from backend.git_webui.bootstrap import create_app as _create_app

_application.create_app = _create_app

if __name__ == "__main__":
    from backend.git_webui.cli import main

    main()
else:
    # Preserve legacy monkey-patching of ``backend.app`` while code is migrated.
    sys.modules[__name__] = _application
