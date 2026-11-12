#!/usr/bin/env python3
"""删除已注销 / 不存在用户遗留的 per-user 模型凭据行。"""

from __future__ import annotations

import argparse
import pathlib
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
for _p in (
    _REPO_ROOT / "packages" / "qwenpaw-data-context" / "src",
    _REPO_ROOT / "packages" / "qwenpaw-data-auth" / "src",
):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# 无鉴权单机模式的运行时身份。
_RUNTIME_ROWS = {("default", "default")}


def _log(tag: str, message: str) -> None:
    print(f"[{tag}] {message}", flush=True)


def _active_users(dsn: str) -> set[tuple[str, str]]:
    from qwenpaw_data_auth.database import get_db, query_all

    with get_db(dsn) as conn:
        rows = query_all(
            conn,
            "SELECT tenant_id, id AS user_id FROM users WHERE status = 'active'",
            (),
        )
    return {(str(r["tenant_id"]), str(r["user_id"])) for r in rows}


def _config_rows(dsn: str) -> list[tuple[str, str, str]]:
    from qwenpaw_data_auth.database import get_db
    from sqlalchemy import select

    from context_manager.model_config.tables import user_model_config

    stmt = select(
        user_model_config.c.tenant_id,
        user_model_config.c.user_id,
        user_model_config.c.updated_at,
    )
    with get_db(dsn) as conn:
        rows = conn.execute(stmt).mappings().all()
    return [
        (str(r["tenant_id"]), str(r["user_id"]), str(r["updated_at"] or ""))
        for r in rows
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn", default="", help="control-plane DSN (default QWENPAW_DATA_DB_URL)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report only; delete nothing"
    )
    args = parser.parse_args()

    from qwenpaw_data_auth.database import control_plane_dsn

    dsn = args.dsn.strip() or control_plane_dsn()
    if not dsn:
        raise SystemExit("QWENPAW_DATA_DB_URL (or --dsn) is required")

    from context_manager.model_config.repository import ModelConfigRepository

    active = _active_users(dsn) | _RUNTIME_ROWS
    rows = _config_rows(dsn)
    orphans = [r for r in rows if (r[0], r[1]) not in active]

    _log("scan", f"model config rows: {len(rows)}")
    _log("scan", f"orphan rows      : {len(orphans)}")
    for tenant_id, user_id, updated_at in orphans:
        _log(
            "orphan",
            f"tenant={tenant_id} user={user_id} updated_at={updated_at or '-'}",
        )

    if not orphans:
        _log("done", "nothing to purge")
        return 0
    if args.dry_run:
        _log("done", "dry run: nothing deleted")
        return 0

    repo = ModelConfigRepository(dsn)
    for tenant_id, user_id, _ in orphans:
        repo.delete(tenant_id, user_id)
    _log("done", f"deleted {len(orphans)} row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
