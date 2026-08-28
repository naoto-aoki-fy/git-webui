"""Immutable runtime configuration and TOML loading."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Settings:
    repository_root: Path
    config_path: Path
    frontend_root: Path
    keep_temporary_workspaces: bool = False
    cors_allowed_origins: tuple[str, ...] = ("*",)
    repository_prefix_destinations: tuple[tuple[str, str], ...] = ()
    git_user_defaults: tuple[tuple[str, str], ...] = ()

    @classmethod
    def load(
        cls,
        *,
        repository_root: Path = Path("repos"),
        config_path: Path = Path("config.toml"),
        frontend_root: Path | None = None,
        keep_temporary_workspaces: bool = False,
        cors_allowed_origins: tuple[str, ...] = ("*",),
    ) -> "Settings":
        config = _load_toml(config_path)
        destinations = _validate_destinations(config.get("repository_prefix_destinations", []))
        users = _validate_git_users(config.get("git_user_defaults", []))
        return cls(
            repository_root=repository_root.expanduser(),
            config_path=config_path,
            frontend_root=frontend_root or Path(__file__).resolve().parents[2] / "frontend",
            keep_temporary_workspaces=keep_temporary_workspaces,
            cors_allowed_origins=cors_allowed_origins,
            repository_prefix_destinations=destinations,
            git_user_defaults=users,
        )


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as config_file:
        try:
            data = tomllib.load(config_file)
        except tomllib.TOMLDecodeError as exc:
            raise RuntimeError(f"Failed to parse configuration file {path}: {exc}") from exc
    return data


def _validate_destinations(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        raise RuntimeError("Configuration file must define 'repository_prefix_destinations' as an array of pairs")
    result: list[tuple[str, str]] = []
    for entry in value:
        if not isinstance(entry, list) or len(entry) != 2 or not all(isinstance(item, str) and item.strip() for item in entry):
            raise RuntimeError("Each repository_prefix_destinations entry must be a [prefix, destination_owner] pair")
        result.append((entry[0], entry[1]))
    return tuple(result)


def _validate_git_users(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        raise RuntimeError("Configuration file must define 'git_user_defaults' as a list")
    result: list[tuple[str, str]] = []
    for entry in value:
        if not isinstance(entry, dict) or not isinstance(entry.get("repository"), str) or not isinstance(entry.get("github_user"), str):
            raise RuntimeError("Each git_user_defaults entry must define repository and github_user")
        result.append((entry["repository"], entry["github_user"]))
    return tuple(result)
