#!/usr/bin/env python3
"""把 models.json 或环境变量中的凭据写入 per-user 模型配置。"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from datetime import datetime, timezone
from typing import Any, Optional

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
for _p in (
    _REPO_ROOT / "packages" / "qwenpaw-data-context" / "src",
    _REPO_ROOT / "packages" / "qwenpaw-data-auth" / "src",
):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

_LEGACY_RELATIVE = pathlib.Path("packages") / "qwenpaw-data-context" / "data" / "models.json"
_VERIFY_LABELS = ("Metric", "Dimension", "Column")
_VERIFY_SAMPLE = 20


def _log(tag: str, message: str) -> None:
    print(f"[{tag}] {message}", flush=True)


# ---------------------------------------------------------------------- #
# 输入来源（models.json / 环境变量）
# ---------------------------------------------------------------------- #


def _legacy_candidates(explicit: Optional[str]) -> list[pathlib.Path]:
    if explicit:
        return [pathlib.Path(explicit).expanduser()]
    out: list[pathlib.Path] = []
    env_path = (os.getenv("MODEL_CONFIG_PATH") or "").strip()
    if env_path:
        out.append(pathlib.Path(env_path).expanduser())
    from context_manager.storage_paths import qwenpaw_data_home

    out.append(qwenpaw_data_home() / "models.json")
    out.append(_REPO_ROOT / _LEGACY_RELATIVE)
    return out


def _resolve_embedding_fallbacks(
    emb: dict[str, Any], llm: dict[str, Any]
) -> dict[str, Any]:
    """补齐 embedding 通道缺失的 base_url / api_key。"""
    from context_manager.config import CFG

    out = dict(emb)
    if not str(out.get("base_url") or "").strip():
        chosen = (
            CFG.embed_openai_base_url
            or str(llm.get("base_url") or "")
            or CFG.openai_base_url
        )
        out["base_url"] = chosen
        _log(
            "legacy",
            f"embedding.base_url was empty; inherited {chosen!r} "
            f"(same chain the old runtime used)",
        )
    if not str(out.get("api_key") or "").strip():
        out["api_key"] = (
            CFG.embed_openai_api_key
            or str(llm.get("api_key") or "")
            or CFG.openai_api_key
        )
        _log("legacy", "embedding.api_key was empty; inherited the LLM key")
    return out


def _load_legacy(
    explicit: Optional[str],
) -> tuple[dict[str, Any], Optional[pathlib.Path]]:
    """返回 (配置, 来源文件)。没有文件时回落 env，来源为 ``None``。"""
    for path in _legacy_candidates(explicit):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"cannot read {path}: {exc}") from exc
        llm_raw = data.get("llm") or {}
        emb_raw = data.get("embedding") or {}
        llm = {
            "base_url": str(llm_raw.get("base_url") or ""),
            "model": str(llm_raw.get("model") or ""),
            "api_key": str(llm_raw.get("api_key") or ""),
        }
        emb = {
            "base_url": str(emb_raw.get("base_url") or ""),
            "model": str(emb_raw.get("model") or ""),
            "api_key": str(emb_raw.get("api_key") or ""),
            "dim": int(emb_raw.get("dim") or 0),
        }
        return {"llm": llm, "embedding": _resolve_embedding_fallbacks(emb, llm)}, path

    from context_manager.config import CFG

    _log("legacy", "no models.json found; falling back to environment variables")
    llm = {
        "base_url": CFG.openai_base_url,
        "model": CFG.llm_model,
        "api_key": CFG.openai_api_key,
    }
    emb = {
        "base_url": CFG.embed_openai_base_url,
        "model": CFG.embed_model,
        "api_key": CFG.embed_openai_api_key,
        "dim": int(CFG.embed_dim),
    }
    return {"llm": llm, "embedding": _resolve_embedding_fallbacks(emb, llm)}, None


def _validate(config: dict[str, Any]) -> None:
    for section in ("llm", "embedding"):
        for key in ("base_url", "model", "api_key"):
            if not str(config[section].get(key) or "").strip():
                raise SystemExit(
                    f"legacy config is incomplete: {section}.{key} is empty. "
                    f"per-user rows cannot be seeded from it."
                )
    if int(config["embedding"]["dim"]) <= 0:
        raise SystemExit("legacy config is incomplete: embedding.dim is not set")


# ---------------------------------------------------------------------- #
# 控制面读取
# ---------------------------------------------------------------------- #


def _active_workspaces(dsn: str) -> list[dict[str, str]]:
    from qwenpaw_data_auth.database import get_db, query_all

    with get_db(dsn) as conn:
        rows = query_all(
            conn,
            """SELECT w.id AS workspace_id, w.tenant_id AS tenant_id
               FROM workspaces w JOIN tenants t ON t.id = w.tenant_id
               WHERE w.status = 'active' AND t.status = 'active'""",
            (),
        )
    return [
        {"workspace_id": str(r["workspace_id"]), "tenant_id": str(r["tenant_id"])}
        for r in rows
    ]


def _active_users(dsn: str) -> list[dict[str, str]]:
    from qwenpaw_data_auth.database import get_db, query_all

    with get_db(dsn) as conn:
        rows = query_all(
            conn,
            """SELECT u.id AS user_id, u.tenant_id AS tenant_id
               FROM users u JOIN tenants t ON t.id = u.tenant_id
               WHERE u.status = 'active' AND t.status = 'active'""",
            (),
        )
    return [
        {"user_id": str(r["user_id"]), "tenant_id": str(r["tenant_id"])} for r in rows
    ]


# ---------------------------------------------------------------------- #
# 图库指纹校验
# ---------------------------------------------------------------------- #


def verify_graph_model(driver: Any, model_name: str) -> tuple[int, int]:
    """抽样比对 ``embedding_hash``，返回 (匹配数, 检查数)。"""
    from context_manager.graph.embeddings import (
        _canonical_text,
        _hash,
        _scoped_fetch_cypher,
    )
    from context_manager.utils import neo4j_session

    matched = 0
    checked = 0
    for label in _VERIFY_LABELS:
        try:
            cypher = _scoped_fetch_cypher(label, None)
            with neo4j_session(driver) as session:
                rows = session.run(cypher).data()
        except Exception as exc:  # noqa: BLE001
            _log("verify", f"{label}: skipped ({exc})")
            continue
        for row in rows:
            stored = str(row.get("embedding_hash") or "")
            if not stored:
                continue
            text = _canonical_text(label, row)
            if not text:
                continue
            checked += 1
            if _hash(model_name, text) == stored:
                matched += 1
            if checked >= _VERIFY_SAMPLE:
                break
        if checked >= _VERIFY_SAMPLE:
            break
    return matched, checked


# ---------------------------------------------------------------------- #
# 主流程
# ---------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed per-user model configuration from models.json or environment variables."
    )
    parser.add_argument(
        "--dsn", default="", help="control-plane DSN (default QWENPAW_DATA_DB_URL)"
    )
    parser.add_argument("--models-json", default="", help="explicit models.json path")
    parser.add_argument(
        "--dry-run", action="store_true", help="report only; write nothing"
    )
    parser.add_argument(
        "--seed-all-users",
        action="store_true",
        help="also seed every active user with these credentials "
        "(everyone temporarily holds a copy of the same key)",
    )
    parser.add_argument(
        "--verify-graph",
        action="store_true",
        help="prove the embedding model actually produced the existing vectors "
        "by recomputing embedding_hash on a sample (requires graph access)",
    )
    parser.add_argument(
        "--skip-default-row",
        action="store_true",
        help="do not seed the default/default row (auth-disabled runtime identity)",
    )
    args = parser.parse_args()

    # 导入 CFG 以加载 .env 中的 QWENPAW_DATA_DB_URL。
    from context_manager.config import CFG  # noqa: F401

    from qwenpaw_data_auth.database import control_plane_dsn

    dsn = args.dsn.strip() or control_plane_dsn()
    if not dsn:
        raise SystemExit("QWENPAW_DATA_DB_URL (or --dsn) is required")

    config, source = _load_legacy(args.models_json.strip() or None)
    _validate(config)

    from context_manager.model_config import embedding_fingerprint, mask_api_key
    from context_manager.model_config import using_builtin_default

    emb = config["embedding"]
    fingerprint = embedding_fingerprint(emb["model"], emb["base_url"], int(emb["dim"]))

    _log("legacy", f"source: {source or '<environment>'}")
    _log(
        "legacy",
        f"llm    : {config['llm']['model']} @ {config['llm']['base_url']} "
        f"key={mask_api_key(config['llm']['api_key'])}",
    )
    _log(
        "legacy",
        f"embed  : {emb['model']} @ {emb['base_url']} dim={emb['dim']} "
        f"key={mask_api_key(emb['api_key'])}",
    )
    _log("legacy", f"embedding fingerprint: {fingerprint}")
    if using_builtin_default():
        _log(
            "warn",
            "QWENPAW_DATA_PREFERENCES_MASTER_SECRET is unset: rows will be encrypted with "
            "the built-in default key, which is public in the repository",
        )

    workspaces = _active_workspaces(dsn)
    users = _active_users(dsn) if args.seed_all_users else []
    _log("plan", f"workspaces to fingerprint: {len(workspaces)}")
    _log(
        "plan",
        f"user rows to seed        : {len(users)}"
        f"{'' if args.seed_all_users else ' (use --seed-all-users to seed)'}",
    )
    if not args.skip_default_row:
        _log("plan", "default/default row     : yes (auth-disabled runtime identity)")

    if args.verify_graph:
        matched, checked = _verify_or_die(emb["model"])
        _log("verify", f"embedding_hash sample: {matched}/{checked} matched")

    if args.dry_run:
        _log("done", "dry run: nothing written")
        return 0

    # 建表：独立 AUTOCOMMIT 连接，建完 dispose 连接池（Hologres 约束）。
    from context_manager.model_config import ensure_schema
    from context_manager.model_config.repository import ModelConfigRepository

    created = ensure_schema(dsn)
    _log("schema", f"created: {', '.join(created) if created else '<already present>'}")

    # DML：新连接，与上面的 DDL 连接分离。
    from context_manager.model_config import SEEDED_PREFIX

    repo = ModelConfigRepository(dsn)
    llm_patch = dict(config["llm"])
    emb_patch = {**emb, "dim": int(emb["dim"])}
    # 播种行：仍使用共享凭据。
    seeded_by = f"{SEEDED_PREFIX}seed"

    seeded = 0
    if not args.skip_default_row:
        repo.upsert_llm("default", "default", updated_by=seeded_by, **llm_patch)
        repo.upsert_embedding("default", "default", updated_by=seeded_by, **emb_patch)
        seeded += 1
    for row in users:
        repo.upsert_llm(
            row["tenant_id"], row["user_id"], updated_by=seeded_by, **llm_patch
        )
        repo.upsert_embedding(
            row["tenant_id"], row["user_id"], updated_by=seeded_by, **emb_patch
        )
        seeded += 1
    _log("seed", f"user rows written: {seeded}")

    for ws in workspaces:
        repo.adopt_space(
            ws["tenant_id"],
            ws["workspace_id"],
            model=emb["model"],
            base_url=emb["base_url"],
            dim=int(emb["dim"]),
            fingerprint=fingerprint,
            adopted_by="migrate_model_config_per_user",
        )
    _log("seed", f"workspace fingerprints written: {len(workspaces)}")

    if source is not None and source.is_file():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        retired = source.with_name(f"{source.name}.migrated-{stamp}")
        source.rename(retired)
        _log("legacy", f"retired {source.name} -> {retired.name} (kept, not deleted)")

    _log("done", "migration complete")
    return 0


def _verify_or_die(model_name: str) -> tuple[int, int]:
    """用当前进程能拿到的 driver 做抽样校验；对不上直接中止迁移。"""
    try:
        from context_manager.config import CFG
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(
            CFG.neo4j_uri, auth=(CFG.neo4j_user, CFG.neo4j_password)
        )
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"--verify-graph needs graph access but the driver could not be built: {exc}"
        ) from exc

    try:
        matched, checked = verify_graph_model(driver, model_name)
    finally:
        try:
            driver.close()
        except Exception:  # noqa: BLE001
            pass

    if checked == 0:
        raise SystemExit(
            "--verify-graph found no embedded nodes to check. Either the graph is "
            "empty (then the fingerprint seed is harmless) or the driver points at "
            "the wrong instance. Rerun without --verify-graph if the graph is empty."
        )
    if matched != checked:
        raise SystemExit(
            f"embedding_hash mismatch: only {matched}/{checked} sampled nodes match "
            f"model {model_name!r}. The legacy config has drifted from the vectors "
            f"actually stored in the graph — seeding this fingerprint would record a "
            f"wrong vector space. Find the model that produced them first."
        )
    return matched, checked


if __name__ == "__main__":
    raise SystemExit(main())
