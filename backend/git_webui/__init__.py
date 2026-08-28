"""Modular application package for git-webui."""

from .bootstrap import create_app
from .settings import Settings

__all__ = ["Settings", "create_app"]
