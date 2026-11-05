"""Installable, domain-configurable QwenPaw-Data browser console."""

from .app import create_app
from .config import ConsoleConfig

__all__ = ["ConsoleConfig", "create_app"]
