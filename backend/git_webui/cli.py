"""Command-line entry point for the backend server."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from aiohttp import web

from .application import DEFAULT_BIND, DEFAULT_CONFIG_PATH, DEFAULT_PORT, DEFAULT_REPO_ROOT, MAX_LINE_SIZE, _parse_port_argument, _resolve_server_listen_config
from .bootstrap import create_app
from .settings import Settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the git-webui backend server.")
    parser.add_argument("--bind", help="Bind address for the backend server.")
    parser.add_argument("--port", type=_parse_port_argument, help="Port number for the backend server.")
    parser.add_argument("--unix-socket", help="AF_UNIX socket path; cannot be combined with --bind or --port.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to config.toml.")
    parser.add_argument("--repo-root", default=str(DEFAULT_REPO_ROOT), help="Repository workspace root.")
    parser.add_argument("--keep-temp", action="store_true", help="Keep temporary workspaces for debugging.")
    parser.add_argument("--serve-frontend", dest="serve_frontend", action="store_true", default=True)
    parser.add_argument("--no-serve-frontend", dest="serve_frontend", action="store_false")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        listen = _resolve_server_listen_config(args.bind, args.port, args.unix_socket)
        origins = tuple(filter(None, (item.strip() for item in os.environ.get("CORS_ALLOW_ORIGIN", "*").split(",")))) or ("*",)
        settings = Settings.load(
            repository_root=Path(args.repo_root),
            config_path=Path(args.config),
            keep_temporary_workspaces=args.keep_temp,
            cors_allowed_origins=origins,
        )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    web.run_app(create_app(settings, serve_frontend=args.serve_frontend), host=listen.host, port=listen.port, path=listen.path, max_line_size=MAX_LINE_SIZE)
