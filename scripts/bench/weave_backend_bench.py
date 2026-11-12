#!/usr/bin/env python3
"""语义编织后端对比 benchmark：Neo4j vs Holo AGE。

同一份 Excel → semantic_config 导入 → weave payload → CM 语义导入管线
（FULL 模式：drop + 7-sheet upsert + caliber 派生 + 统计），分别在
Neo4j（本地 docker）与 Holo AGE（HOLO_AGE_* 指定的 Hologres 实例）上跑 N 轮，按阶段计时。

用法：
    packages/qwenpaw-data-context/.venv/bin/python scripts/bench/weave_backend_bench.py --excel <workbook.xlsx> [--runs 3] [--with-embed]

说明：
- Neo4j = 本机 docker（~0.1ms RTT）；Holo = HOLO_AGE_* 指定的 Hologres 实例（含网络 RTT）。
  网络差异是生产现实的一部分，结果解读时注意。
- embedding 重建（index_embeddings）走模型 API，默认关闭（--with-embed 打开），
  因为它对两个后端是同样的模型调用，会淹没图写入差异。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


_load_dotenv(REPO / ".env")

# 临时 semantic_config 库（不碰线上控制面）
_TMP_DB = Path(tempfile.mkdtemp(prefix="weave_bench_")) / "semantic.db"
os.environ["QWENPAW_DATA_DB_URL"] = f"sqlite+aiosqlite:///{_TMP_DB}"

_PKG_SRC = REPO / "packages" / "qwenpaw-data-context" / "src"
if str(_PKG_SRC) not in sys.path:
    sys.path.insert(0, str(_PKG_SRC))

BENCH_TENANT = "bench_tenant"
BENCH_USER = "bench_user"
BENCH_DATASOURCE_ID = "benchmark-spec01"
WS_NEO4J = "benchwsneo4j"
WS_HOLO = "benchwsholo"


async def build_payload(excel_path: Path) -> tuple[dict, str]:
    from qwenpaw_data_auth.principal import Principal
    from semantic_config.db import init_db, now_iso
    from semantic_config.engine import get_session_factory
    from semantic_config.services import excel_import_service, weave_assembler
    from semantic_config.tables import datasource
    from sqlalchemy import insert, select

    await init_db()
    SessionLocal = get_session_factory()
    principal = Principal(
        user_id=BENCH_USER,
        tenant_id=BENCH_TENANT,
        workspace_ids=[WS_HOLO],
        active_workspace_id=WS_HOLO,
        role="ws_admin",
    )
    content = excel_path.read_bytes()
    async with SessionLocal() as db:
        existing = await db.scalar(
            select(datasource.c.datasource_id).where(
                datasource.c.datasource_id == BENCH_DATASOURCE_ID
            )
        )
        if existing is None:
            await db.execute(
                insert(datasource).values(
                    datasource_id=BENCH_DATASOURCE_ID,
                    datasource_name=BENCH_DATASOURCE_ID,
                    datasource_type="benchmark",
                    config=None,
                    tenant_id=BENCH_TENANT,
                    workspace_id=WS_HOLO,
                    created_by=BENCH_USER,
                    updated_by=BENCH_USER,
                    is_deleted=0,
                    created_at=now_iso(),
                    updated_at=now_iso(),
                )
            )
            print(f"[payload] pre-inserted datasource row: {BENCH_DATASOURCE_ID}")
        await db.commit()

        summary = await excel_import_service.import_excel(
            db,
            excel_path.name,
            content,
            principal,
            datasource_id=BENCH_DATASOURCE_ID,
        )
        print(
            f"[payload] excel import summary: {json.dumps(summary, ensure_ascii=False)[:300]}"
        )
        if not summary.get("success", False):
            raise RuntimeError(
                f"excel 导入失败: {json.dumps(summary, ensure_ascii=False)[:500]}"
            )
        payload = await weave_assembler.assemble(
            db,
            BENCH_DATASOURCE_ID,
            "BENCH_TASK",
            BENCH_TENANT,
            WS_HOLO,
            drop_semantic_first=True,
        )
    n_domains = len(payload.get("semantic", {}).get("domains", []))
    print(f"[payload] datasource_id={BENCH_DATASOURCE_ID!r} domains={n_domains}")
    return payload, BENCH_DATASOURCE_ID


def expand_rows(request, datasource_id: str) -> dict[str, list[dict]]:
    """与 semantic_import_service._run_semantic_import_impl 一致的 sheet 展开。"""
    rows: dict[str, list[dict]] = {
        name: []
        for name in (
            "biz_domain",
            "dataset",
            "dataset_column",
            "dimension",
            "dataset_dimension",
            "metric",
            "metric_formula",
        )
    }
    for domain in request.semantic.domains:
        common = {"datasource_id": datasource_id, "domain_name": domain.name}
        rows["biz_domain"].append(
            {
                **common,
                "display_name": domain.display_name,
                "description": domain.description,
                "aliases": domain.aliases,
            }
        )
        for dataset in domain.datasets:
            rows["dataset"].append(
                {
                    **common,
                    "dataset_name": dataset.name,
                    "dataset_comment": dataset.description,
                    "dataset_type": dataset.dataset_type,
                    "sql_content": dataset.sql,
                    "parents": "$$$".join(dataset.parents),
                }
            )
            for column in dataset.columns:
                rows["dataset_column"].append(
                    {
                        **common,
                        "dataset_name": dataset.name,
                        "column_name": column.name,
                        "data_type": column.data_type,
                        "is_primary": column.is_primary,
                        "is_nullable": column.is_nullable,
                        "column_comment": column.comment,
                        "column_name_cn": column.name_cn,
                        "column_type": column.column_type,
                        "column_enums": column.enums or [],
                        "samples": column.samples or [],
                        "dimension_type": column.dimension_type,
                    }
                )
        for dimension in domain.dimensions:
            rows["dimension"].append(
                {
                    **common,
                    "dimension_name": dimension.name,
                    "description": dimension.description,
                    "parent_name": dimension.parent_dimension,
                    "depth": dimension.hierarchy_level,
                    "synonyms": dimension.aliases,
                    "is_visible": dimension.is_display_dimension,
                    "is_attribution": dimension.is_contribution_dimension,
                    "enums": [v.model_dump() for v in dimension.values],
                }
            )
            for binding in dimension.bindings:
                rows["dataset_dimension"].append(
                    {
                        **common,
                        "dataset_name": binding.dataset,
                        "dimension_name": dimension.name,
                        "calculate_expr": binding.calculate_expr,
                        "dimension_type": binding.binding_type,
                        "data_type": binding.data_type,
                    }
                )
        for metric in domain.metrics:
            rows["metric"].append(
                {
                    **common,
                    "metric_name": metric.name,
                    "description": metric.description,
                    "unit": metric.unit,
                    "is_polaris": metric.is_north_star,
                    "show_distribution": metric.is_display_distribution,
                    "is_visible": metric.is_display,
                    "synonyms": metric.aliases,
                    "tags": metric.tags,
                }
            )
            for formula in metric.formulas:
                rows["metric_formula"].append(
                    {
                        **common,
                        "metric_name": metric.name,
                        "dataset_name": formula.dataset,
                        "formula": formula.formula,
                        "formula_evidence": formula.formula_evidence,
                        "date_range": formula.date_range,
                        "derived_from": [formula.derived_from]
                        if formula.derived_from
                        else [],
                    }
                )
    return rows


# ---------------------------------------------------------------------- #
# 单轮 weave（FULL：drop + upsert + derive + stats），按阶段计时
# ---------------------------------------------------------------------- #
def run_weave_once(access, request, ds_id: str, *, with_embed: bool) -> dict:
    from context_manager.graph.semantic_import_service import (
        drop_semantic_for_datasource,
    )
    from context_manager.graph.semantic_incremental_write import upsert_rows
    from context_manager.graph.semantic_derive import derive_calibers_from_formulas
    from context_manager.graph.stats import count_semantic_nodes

    out: dict = {"sheets": {}}
    access.require("write_global_mg")

    t0 = time.monotonic()
    dropped = drop_semantic_for_datasource(access, ds_id)
    out["drop"] = time.monotonic() - t0
    out["dropped_domains"] = dropped

    rows = expand_rows(request, ds_id)
    t0 = time.monotonic()
    for sheet, sheet_rows in rows.items():
        ts = time.monotonic()
        n = upsert_rows(access, sheet, sheet_rows, datasource_id=ds_id)
        out["sheets"][sheet] = {"n": n, "t": round(time.monotonic() - ts, 3)}
    out["upsert"] = time.monotonic() - t0

    t0 = time.monotonic()
    derive_calibers_from_formulas(access)
    out["derive"] = time.monotonic() - t0

    if with_embed:
        from context_manager.graph.embeddings import index_embeddings

        t0 = time.monotonic()
        index_embeddings(
            access,
            scope=("metric", "dimension", "dataset", "datasetcolumn"),
            ensure_indexes=False,
        )
        out["embed"] = time.monotonic() - t0

    t0 = time.monotonic()
    counts = count_semantic_nodes(access)
    out["stats"] = time.monotonic() - t0
    out["node_counts"] = counts
    out["total"] = sum(
        out[k] for k in ("drop", "upsert", "derive", "stats") if k in out
    ) + out.get("embed", 0.0)
    return out


# ---------------------------------------------------------------------- #
# 后端装配
# ---------------------------------------------------------------------- #
def build_backends():
    from context_manager.config import CFG
    from context_manager.graph.backends.neo4j_backend import Neo4jBackend
    from context_manager.graph.backends.holo_age_backend import HoloAgeBackend
    from context_manager.graph.backends.registry import get_manager
    import neo4j as neo4j_lib

    neo4j_backend = Neo4jBackend(
        uri=CFG.neo4j_uri,
        user=CFG.neo4j_user,
        password=CFG.neo4j_password,
        default_database=CFG.neo4j_database,
    )
    neo4j_driver = neo4j_lib.GraphDatabase.driver(
        CFG.neo4j_uri,
        auth=(CFG.neo4j_user, CFG.neo4j_password),
    )
    holo_backend = HoloAgeBackend(
        host=CFG.holo_age_host,
        port=CFG.holo_age_port,
        user=CFG.holo_age_user,
        password=CFG.holo_age_password,
        database=CFG.holo_age_database,
        graph_name=CFG.holo_age_graph_name,
    )
    mgr = get_manager()
    mgr.register("neo4j", neo4j_backend)
    mgr.register("holo_age", holo_backend)
    return mgr, neo4j_driver, neo4j_backend, holo_backend


def holo_rtt_ms() -> float:
    """Holo AGE 实例单次往返参考值（SELECT 1）。"""
    import psycopg

    conn = psycopg.connect(
        host=os.environ["HOLO_AGE_HOST"],
        port=int(os.environ["HOLO_AGE_PORT"]),
        user=os.environ["HOLO_AGE_USER"],
        password=os.environ["HOLO_AGE_PASSWORD"],
        dbname=os.environ["HOLO_AGE_DATABASE"],
        connect_timeout=10,
    )
    try:
        ts = []
        for _ in range(5):
            t0 = time.monotonic()
            conn.execute("SELECT 1").fetchone()
            ts.append((time.monotonic() - t0) * 1000)
        return round(statistics.median(ts), 2)
    finally:
        conn.close()


# ---------------------------------------------------------------------- #
# 主流程
# ---------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3, help="每个后端计时轮数")
    ap.add_argument("--excel", type=Path, required=True, help="语义 Excel 工作簿路径")
    ap.add_argument("--with-embed", action="store_true", help="把 embedding 重建计入")
    args = ap.parse_args()

    print(
        f"[env] excel={args.name if False else args.excel.name} runs={args.runs} with_embed={args.with_embed}"
    )
    payload, ds_id = asyncio.run(build_payload(args.excel))

    from context_manager.api.import_models import SemanticImportRequest

    normalized = dict(payload)
    # 与 weave_task_service._run_import_in_process 一致：datasource_id 缺省回退 name
    normalized.setdefault("datasource_id", normalized.get("datasource_name", ""))
    request = SemanticImportRequest.model_validate(normalized)

    mgr, neo4j_driver, neo4j_backend, holo_backend = build_backends()

    from qwenpaw_data_auth.principal import Principal
    from context_manager.api.graph_access import ScopedGraphAccess

    try:
        rtt = holo_rtt_ms()
    except Exception as e:  # noqa: BLE001
        rtt = -1.0
        print(f"[warn] holo RTT 探测失败: {e}")
    print(f"[env] holo SELECT-1 RTT ≈ {rtt} ms（本地 Neo4j 为 docker 内网）")

    results: dict[str, list[dict]] = {"neo4j": [], "holo_age": []}

    phases = [
        ("neo4j", WS_NEO4J, neo4j_driver),
        ("holo_age", WS_HOLO, neo4j_driver),  # holo 活跃时 driver 被忽略
    ]
    for backend_name, ws, driver in phases:
        mgr.switch(backend_name)
        # 离线脚本里 derive/stats 等经 utils.neo4j_session 开的 Holo 会话
        # 不携带 principal 上下文，靠 GRAPH_WORKSPACE 环境变量路由到本阶段图
        os.environ["GRAPH_WORKSPACE"] = ws
        principal = Principal(
            user_id=BENCH_USER,
            tenant_id=BENCH_TENANT,
            workspace_ids=[ws],
            active_workspace_id=ws,
            role="ws_admin",
        )
        access = ScopedGraphAccess(principal, driver)

        # 预热（不计）：Holo 幂等建图 DDL / Neo4j 连接池首握
        warm = run_weave_once(access, request, ds_id, with_embed=False)
        print(
            f"[{backend_name}] warmup total={warm['total']:.2f}s "
            f"(drop={warm['drop']:.2f} upsert={warm['upsert']:.2f}) "
            f"counts={json.dumps(warm['node_counts'], ensure_ascii=False)[:160]}"
        )

        for i in range(args.runs):
            r = run_weave_once(access, request, ds_id, with_embed=args.with_embed)
            results[backend_name].append(r)
            print(
                f"[{backend_name}] run#{i + 1} total={r['total']:.2f}s "
                f"drop={r['drop']:.2f} upsert={r['upsert']:.2f} "
                f"derive={r['derive']:.2f} stats={r['stats']:.3f}"
                + (f" embed={r['embed']:.2f}" if args.with_embed else "")
            )

    # ------------------------------------------------------------------ #
    # 汇总
    # ------------------------------------------------------------------ #
    print("\n" + "=" * 72)

    def med(backend: str, key: str) -> float:
        vals = [r[key] for r in results[backend] if key in r]
        return statistics.median(vals) if vals else float("nan")

    stages = ["drop", "upsert", "derive", "stats"] + (
        ["embed"] if args.with_embed else []
    )
    header = f"{'stage':<10}" + "".join(f"{b:>22}" for b in ("neo4j", "holo_age"))
    print(header)
    for st in stages:
        line = f"{st:<10}"
        for b in ("neo4j", "holo_age"):
            vals = [r[st] for r in results[b] if st in r]
            line += f"{statistics.median(vals):>14.2f}s " if vals else f"{'-':>15} "
        print(line)
    line = f"{'TOTAL':<10}"
    for b in ("neo4j", "holo_age"):
        line += f"{med(b, 'total'):>14.2f}s "
    print(line)

    # 逐 sheet upsert 中位数对比
    print("\nupsert 分 sheet（中位数，秒）：")
    sheet_names = list(results["neo4j"][0]["sheets"].keys())
    print(f"{'sheet':<20}" + "".join(f"{b:>14}" for b in ("neo4j", "holo_age")))
    for sh in sheet_names:
        line = f"{sh:<20}"
        for b in ("neo4j", "holo_age"):
            vals = [r["sheets"][sh]["t"] for r in results[b]]
            line += f"{statistics.median(vals):>13.2f}s"
        print(line)

    # 正确性核对：最后一轮的节点计数
    print("\n节点计数核对（最后一轮 stats）：")
    for b in ("neo4j", "holo_age"):
        print(f"  {b}: {json.dumps(results[b][-1]['node_counts'], ensure_ascii=False)}")

    print(f"\n每后端 runs={args.runs}（另含 1 轮 warmup 未计入）。")


if __name__ == "__main__":
    main()
