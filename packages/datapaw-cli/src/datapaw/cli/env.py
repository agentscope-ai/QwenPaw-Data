"""DataPaw CLI dotenv resolution."""

from __future__ import annotations

import os
from pathlib import Path

DATAPAW_ENV_FILE = "DATAPAW_ENV_FILE"


def datapaw_repo_root() -> Path:
    """Return the monorepo root when running the CLI from a checkout."""

    candidates = [Path(__file__).resolve().parent, Path.cwd().resolve()]
    for start in candidates:
        for path in (start, *start.parents):
            if (path / "pyproject.toml").is_file() and (
                path / "packages" / "datapaw-cli"
            ).is_dir():
                return path
    return Path.cwd().resolve()


def datapaw_env_file() -> Path:
    """Return the configured root dotenv file for the CLI."""

    raw = (os.getenv(DATAPAW_ENV_FILE) or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return datapaw_repo_root() / ".env"


def load_datapaw_env(*, override: bool = False) -> Path:
    """Load the CLI dotenv file without consulting package-local env files."""

    path = datapaw_env_file()
    try:
        from dotenv import load_dotenv
    except ImportError:
        return path
    load_dotenv(path, override=override)
    return path
