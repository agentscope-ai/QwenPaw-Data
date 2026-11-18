"""Discover historical local layouts without connecting to, or changing, a database."""

from __future__ import annotations

import logging
from pathlib import Path

from qwenpaw_data_auth.database import default_database_path


# Stable IDs identify a source layout, not proof that an import has run.
_LOCAL_SQLITE_LAYOUTS = (("oss-host-sqlite-v0.3", Path("host/host.db")),)


def discover_local_stores(home: Path) -> dict:
    home = home.expanduser().absolute()
    target = default_database_path(home)
    findings = []
    for layout, relative in _LOCAL_SQLITE_LAYOUTS:
        source = home / relative
        if source.is_file():
            findings.append({"layout": layout, "candidate": str(source)})
    return {
        "report_version": 1,
        "home": str(home),
        "default_database": str(target),
        "default_database_exists": target.is_file(),
        "candidates": findings,
    }


def warn_about_local_stores(home: Path) -> None:
    report = discover_local_stores(home)
    target = Path(report["default_database"])
    for item in report["candidates"]:
        source = Path(item["candidate"])
        if target.is_file() and source.samefile(target):
            raise RuntimeError(
                f"Local database {target} aliases historical candidate {source}; "
                "refusing to initialize it. Back up before upgrading."
            )
        logging.getLogger(__name__).warning(
            "Possible older OSS database (%s): %s. This Host uses %s; "
            "the old file is not imported. See 'qwenpaw-data migration discover'.",
            item["layout"], source, target,
        )
