#!/usr/bin/env python3
"""把 CM 与 Host 的凭据密文从旧主密钥重加密到新主密钥。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import pathlib
import sys

from cryptography.fernet import Fernet, InvalidToken

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
for _p in (
    _REPO_ROOT / "packages" / "qwenpaw-data-context" / "src",
    _REPO_ROOT / "packages" / "qwenpaw-data-auth" / "src",
):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

_ENC = "ENC:"

# 需要重加密的 (表, 主键列, 密文列)。两侧密文格式相同，一起处理。
_TARGETS = (
    ("cm_user_model_config", ("tenant_id", "user_id"), "llm_api_key_enc"),
    ("cm_user_model_config", ("tenant_id", "user_id"), "embed_api_key_enc"),
    ("user_providers", ("user_id", "provider_id"), "api_key_enc"),
)


def _log(tag: str, message: str) -> None:
    print(f"[{tag}] {message}", flush=True)


def _fernet(secret: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(bytes.fromhex(secret)[:32]))


def _fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii")).hexdigest()[:8]


def _validate_secret(value: str, label: str) -> str:
    raw = (value or "").strip()
    if len(raw) != 64:
        raise SystemExit(f"{label} must be 64 hex characters")
    try:
        bytes.fromhex(raw)
    except ValueError as exc:
        raise SystemExit(f"{label} must be hex-encoded") from exc
    return raw


def _current_secret() -> str:
    """Return the master secret Host and Context use now, without generating one."""
    from context_manager.settings.model import ModelSettings
    from context_manager.settings.paths import PathsSettings

    secret = ModelSettings().preferences_master_secret.strip()
    if secret:
        return secret
    key_path = PathsSettings().home / "preferences.key"
    if not key_path.is_file():
        raise SystemExit(
            "no current master secret: set QWENPAW_DATA_PREFERENCES_MASTER_SECRET, "
            f"create {key_path}, or pass --old-secret"
        )
    return key_path.read_text(encoding="utf-8").strip()


def _table_exists(conn, name: str) -> bool:
    from sqlalchemy import inspect

    return name in set(inspect(conn.engine).get_table_names())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn", default="", help="control-plane DSN (default QWENPAW_DATA_DB_URL)"
    )
    parser.add_argument(
        "--old-secret",
        default="",
        help=(
            "current master secret; defaults to QWENPAW_DATA_PREFERENCES_MASTER_SECRET "
            "(legacy QWENPAW_DATA_PREFS_MASTER_SECRET), else "
            "<QWENPAW_DATA_HOME>/preferences.key"
        ),
    )
    parser.add_argument("--new-secret", required=True, help="64 hex chars")
    parser.add_argument(
        "--dry-run", action="store_true", help="report only; write nothing"
    )
    args = parser.parse_args()

    from qwenpaw_data.context.env import load_qwenpaw_data_env
    from qwenpaw_data_auth.database import _get_engine, control_plane_dsn

    load_qwenpaw_data_env()  # 加载 .env

    dsn = args.dsn.strip() or control_plane_dsn()
    if not dsn:
        raise SystemExit("QWENPAW_DATA_DB_URL (or --dsn) is required")

    old = _validate_secret(args.old_secret or _current_secret(), "--old-secret")
    new = _validate_secret(args.new_secret, "--new-secret")
    if old == new:
        raise SystemExit("--new-secret is identical to the old one; nothing to do")

    old_f, new_f = _fernet(old), _fernet(new)
    _log(
        "key",
        f"old fingerprint={_fingerprint(old)} new fingerprint={_fingerprint(new)}",
    )
    if not args.old_secret:
        _log(
            "key",
            "old secret not given; using QWENPAW_DATA_PREFERENCES_MASTER_SECRET "
            "or <QWENPAW_DATA_HOME>/preferences.key",
        )

    engine = _get_engine(dsn)
    from sqlalchemy import text

    totals = {"rotated": 0, "already_new": 0, "unreadable": 0, "empty": 0}

    with engine.connect() as conn:
        for table, keys, column in _TARGETS:
            if not _table_exists(conn, table):
                _log("skip", f"{table}.{column}: 表不存在")
                continue
            key_list = ", ".join(keys)
            rows = (
                conn.execute(text(f"SELECT {key_list}, {column} FROM {table}"))
                .mappings()
                .all()
            )

            rotated = already = unreadable = empty = 0
            for row in rows:
                value = str(row[column] or "")
                if not value:
                    empty += 1
                    continue
                if not value.startswith(_ENC):
                    unreadable += 1
                    continue
                token = value[len(_ENC) :].encode("ascii")
                try:
                    plaintext = old_f.decrypt(token)
                except (InvalidToken, ValueError):
                    # 已是新密钥或无法用旧密钥解密。
                    try:
                        new_f.decrypt(token)
                    except (InvalidToken, ValueError):
                        unreadable += 1
                    else:
                        already += 1
                    continue

                if not args.dry_run:
                    where = " AND ".join(f"{k} = :{k}" for k in keys)
                    params = {k: row[k] for k in keys}
                    params["value"] = _ENC + new_f.encrypt(plaintext).decode("ascii")
                    conn.execute(
                        text(f"UPDATE {table} SET {column} = :value WHERE {where}"),
                        params,
                    )
                rotated += 1

            _log(
                table,
                f"{column}: 待重加密 {rotated}，已是新密钥 {already}，"
                f"解不开 {unreadable}，空 {empty}",
            )
            totals["rotated"] += rotated
            totals["already_new"] += already
            totals["unreadable"] += unreadable
            totals["empty"] += empty

        # 指纹列与密文一起更新。
        if not args.dry_run and _table_exists(conn, "cm_user_model_config"):
            conn.execute(
                text(
                    "UPDATE cm_user_model_config SET key_fingerprint = :fp "
                    "WHERE llm_api_key_enc IS NOT NULL OR embed_api_key_enc IS NOT NULL"
                ),
                {"fp": _fingerprint(new)},
            )
        if not args.dry_run:
            conn.commit()

    if totals["unreadable"]:
        _log(
            "warn",
            f"{totals['unreadable']} 行用两把钥匙都解不开——它们是第三把密钥加的，"
            f"或者已损坏。这些用户需要重新填写凭据",
        )
    if args.dry_run:
        _log("done", f"dry run：会重加密 {totals['rotated']} 行，未写入")
        return 0
    _log(
        "done",
        f"已重加密 {totals['rotated']} 行；现在把新主密钥配到两侧部署"
        "（QWENPAW_DATA_PREFERENCES_MASTER_SECRET 或 <QWENPAW_DATA_HOME>/preferences.key）再起服",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
