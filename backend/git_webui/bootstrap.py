"""Application composition root.

Only this module turns immutable settings into concrete runtime dependencies.
"""

from __future__ import annotations

from aiohttp import web

from . import application
from .settings import Settings
from .web.keys import SETTINGS_KEY


def create_app(settings: Settings | None = None, *, serve_frontend: bool = True) -> web.Application:
    settings = settings or Settings.load()
    settings.repository_root.mkdir(parents=True, exist_ok=True)
    application.configure(settings)

    app = application.create_web_app(serve_frontend=serve_frontend)
    app[SETTINGS_KEY] = settings
    return app
