"""Shared QwenPaw Data dotenv resolution."""

from __future__ import annotations

import os
from pathlib import Path

QWENPAW_DATA_ENV_FILE = "QWENPAW_DATA_ENV_FILE"


def qwenpaw_data_repo_root() -> Path:
    """Return the monorepo root when running from a checkout."""

    candidates = [Path(__file__).resolve().parent, Path.cwd().resolve()]
    for start in candidates:
        for path in (start, *start.parents):
            if (path / "pyproject.toml").is_file() and (
                path / "packages" / "qwenpaw-data-context"
            ).is_dir():
                return path
    return Path.cwd().resolve()


def qwenpaw_data_env_file() -> Path:
    """Return the configured QwenPaw Data env file path."""

    raw = (os.getenv(QWENPAW_DATA_ENV_FILE) or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return qwenpaw_data_repo_root() / ".env"


def load_qwenpaw_data_env(*, override: bool = False) -> Path:
    """Load the QwenPaw Data env file without consulting package-local env files."""

    path = qwenpaw_data_env_file()
    try:
        from dotenv import load_dotenv
    except ImportError:
        return path
    load_dotenv(path, override=override)
    return path
