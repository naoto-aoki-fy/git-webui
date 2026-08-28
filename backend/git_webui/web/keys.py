"""Typed aiohttp application keys shared by composition and handlers."""

from aiohttp import web

from ..settings import Settings

SETTINGS_KEY = web.AppKey("settings", Settings)
SSE_CLIENTS_KEY = web.AppKey("sse_clients", dict)
SUBMISSIONS_KEY = web.AppKey("submissions", dict)
FRONTEND_ROOT_KEY = web.AppKey("frontend_root", object)
FORCED_PAYLOAD_KEY = web.RequestKey("forced_payload", dict)
