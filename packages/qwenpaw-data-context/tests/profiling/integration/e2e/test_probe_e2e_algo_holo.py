"""后端 E2E（Hologres 离线回放 · 第一、二步）：载入内网录制的表快照 / 查询日志，跑 BIZ_DOMAIN 探查，再完成 DATASET 抽取并核对 dataset / dataset_field。

覆盖到 DATASET_FIELDS 为止，口径与字典阶段刻意不做：

  ⓪ 载入：把 ``data/profile/postgresql-84ad065b/``（生产 AlgoProvider 路径在
     内网跑出来的 ``source_table`` 134 张表 + ``query_log_raw`` 1000 条 Hologres
     系统日志）**拷贝**进 E2E 的 out_dir；断言两级快照的指纹与录制一致 ——
     provider 装配时 ``SourceContext.load()`` 本地优先、指纹命中即不发远程
     查询，所以数据源 connection 是一组永远不会被连接的占位值（PG 执行器
     构造惰性，只在真的执行 SQL 时才 connect）。
  ① BIZ_DOMAIN 探查：真实 AlgoProvider → DOMAIN 任务（join 聚类 + LLM 归域）
     → DbProbeSink → staging_biz_domain。呈现：每个域的名称 / 描述 /
     成员表，join 证据与 cluster 一致性，表覆盖率；结果同时落
     ``{out_dir}/domain_result.md`` 与 ``domain_result.json``。
  ② BIZ_DOMAIN confirm 全部 → complete（promote → 正式 domain）—— DATASET 的前置。
  ③ DATASET 抽取：真实 AlgoProvider → DATASET 任务 → staging_dataset；确认后再探查
     DATASET_FIELDS → staging_dataset_field。两条来源：直通（AlgoConfig 默认白名单 ``view%`` /
     ``dataset%`` 且已归域的表，列元数据原样继承）与日志式（1000 条录制
     日志里重复 ≥3 次的 SQL 模板 → LLM 起名/描述，字段按投影继承）。
     核对：直通集合 == 白名单（未归域表在已有 Domain 内补分配）；每张直通表的字段集合 / data_type /
     is_primary / is_nullable / 注释与录制列面逐项相等；日志式 dataset 的
     source 是 SQL、有描述、至少一个字段。结果落 ``dataset_result.md/json``。
  ④ DATASET_FIELDS confirm 全部 → complete → promote：正式 ``dataset`` /
     ``dataset_column_meta`` 行数与 staging 对账。

参与指纹的配置必须与录制时一致（枚举探查关、exclude ``tmp%``、分区归并
开；日志层 dbname 空、max_count 默认）。生产默认时间窗会逐日滚动，离线
fixture 则在本测试的 env 覆盖里显式钉住录制日期。产出语言走默认 zh
（示例业务库，注释是中文）。

费用：DOMAIN 一次（覆盖率不足时重试一次）+ DATASET 日志式起名一次真实 LLM
调用（直通不走 LLM）；响应缓存落 ``data/profile/e2e/e2e-holo/llm/``，之后重跑免费。

运行::

    cd packages/qwenpaw-data-context
    uv run --no-sync --with pytest python -m pytest \
        tests/profiling/integration/e2e/test_probe_e2e_algo_holo.py -s --log-cli-level=INFO
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

import pytest

log = logging.getLogger("test_probe_e2e_holo")

_PKG = Path(__file__).resolve().parents[4]  # packages/qwenpaw-data-context
_DB = "holo"
#: 内网录制的两级快照（生产 AlgoProvider 路径跑出来的缓存）。只读，测试拷贝后使用
_FIXTURE = _PKG / "data" / "profile" / "postgresql-84ad065b"
_HOME_ROOT = Path(tempfile.gettempdir()) / "qwenpaw-data-profiling-e2e-home"
_OUT_ROOT = _HOME_ROOT / "t1" / "w1" / ".cm" / "profile"
_DS_ID = f"e2e-{_DB}"  # 固定 → 跨运行共享 LLM 缓存
#: 占位连接：PG 执行器要求 host/dbname/user 三键才能构造，但构造不连接；
#: 快照指纹命中即不执行任何 SQL，这组值永远不会被用来建连
_CONNECTION = {
    "host": "offline.invalid",
    "port": 8099,
    "dbname": "offline_db",
    "user": "offline-replay",
}

#: 参与快照指纹的项除录制时间窗外不覆盖；动态生产默认不能用于离线 fixture
_ENV_OVERRIDES = {
    "EXECUTE_SQL_PATH": "",  # 自建 PG 执行器（惰性，不会真连）
    "QWENPAW_DATA_HOME": str(_HOME_ROOT),  # 在线 Provider 的稳定缓存目录
    "STORE_OUT_DIR": str(_OUT_ROOT),  # 离线回读同一目录
    "PARSER_DIALECT": "postgres",
    "PARSER_TEMPLATE_MIN_COUNT": "3",  # 1000 条日志：重复 10 次的模板为 0，3 次的有 6 个
    "INPUT_QUERY_LOG_START_TS": "20260601",  # 录制窗口（包含）
    "INPUT_QUERY_LOG_END_TS": "20260801",  # 录制窗口（包含）
}


# ===========================================================================
# 工具
# ===========================================================================


def _source_tables() -> set[str]:
    """录制的表面（``source_table/table_meta``），与 provider 命中缓存后看到的完全相同。"""
    import pandas as pd

    meta = pd.read_parquet(_FIXTURE / "source_table" / "table_meta.parquet")
    return {str(n) for n in meta["table_name"]}


def _table_comments() -> dict[str, str]:
    import pandas as pd

    meta = pd.read_parquet(_FIXTURE / "source_table" / "table_meta.parquet")
    return {
        str(r.table_name): (
            ""
            if r.table_comment is None or str(r.table_comment) == "nan"
            else str(r.table_comment)
        )
        for r in meta.itertuples(index=False)
    }


def _like_matches(names: set[str], patterns: tuple[str, ...]) -> set[str]:
    """直通白名单的 SQL LIKE 语义（``%`` 任意串、``_`` 单字符，大小写不敏感）。"""
    import fnmatch

    out: set[str] = set()
    for pattern in patterns:
        glob = pattern.lower().replace("%", "*").replace("_", "?")
        out |= {n for n in names if fnmatch.fnmatchcase(n.lower(), glob)}
    return out


def _member_tables_of(row: dict) -> set[str]:
    """staging_biz_domain.member_tables 是 CandidateMemberTable 的 JSON 串。"""
    raw = row.get("member_tables")
    if not raw:
        return set()
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return set()
    return {
        str(i.get("source_table")).strip()
        for i in items
        if isinstance(i, dict) and i.get("source_table")
    }


def _wipe_context_snapshots(out_dir: Path) -> None:
    """清掉上一轮的 context 快照（本地 parquet 会被当作 prior 读回）；只留 llm/ —— 响应缓存，钱。"""
    if not out_dir.is_dir():
        return
    for child in out_dir.iterdir():
        if child.is_dir() and child.name != "llm":
            shutil.rmtree(child)
            log.info("  已清理上一轮快照目录 %s", child.name)


def _restore_fixture(out_dir: Path) -> None:
    """把录制的两级快照拷进 E2E out_dir（不动 fixture 本身）。"""
    for ctx in ("source_table", "query_log_raw"):
        target = out_dir / ctx
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(_FIXTURE / ctx, target)
        log.info("  已拷贝录制快照 %s → %s", ctx, target)


async def _drain(orchestrator) -> None:
    """等待所有后台探查任务结束。"""
    for _ in range(10_000):
        tasks = list(orchestrator._REGISTRY.values())
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)


# ===========================================================================
# 主链路（DOMAIN → DATASET）
# ===========================================================================


def test_e2e_holo_replay_domain_dataset():
    if (
        not (_FIXTURE / "source_table").is_dir()
        or not (_FIXTURE / "query_log_raw").is_dir()
    ):
        pytest.skip(f"Hologres 录制快照缺失: {_FIXTURE}")

    from profiling.algo.inputs.query_log_postgres import (
        QUERY_LOG_WINDOW_SEMANTICS,
    )

    raw_manifest = json.loads(
        (_FIXTURE / "query_log_raw" / "_meta" / "snapshot.json").read_text()
    )
    recorded_semantics = raw_manifest.get("fingerprint", {}).get(
        "query_log_window_semantics"
    )
    if recorded_semantics != QUERY_LOG_WINDOW_SEMANTICS:
        pytest.skip(
            "Hologres 录制快照仍是旧的结束日谓词语义；"
            "请用当前代码重新录制 query_log_raw 后再回放"
        )

    # -- env 注入 + AlgoConfig 基线重置（基线是进程级 lru_cache）----------
    saved_env = {k: os.environ.get(k) for k in _ENV_OVERRIDES}
    os.environ.update(_ENV_OVERRIDES)
    from profiling.algo import config as algo_config

    algo_config._baseline.cache_clear()
    probe_cfg = algo_config.AlgoConfig.build(datasource_id=_DS_ID)
    if not probe_cfg.llm_enabled:
        for key, value in saved_env.items():
            os.environ.pop(key, None) if value is None else os.environ.__setitem__(
                key, value
            )
        algo_config._baseline.cache_clear()
        pytest.skip("缺少 OPENAI_API_KEY —— 真 LLM 链路无法运行")

    # -- LLM 真实调用计数（缓存命中不进 _call_sync）------------------------
    from profiling.algo.handlers.llm import OpenAiCompatClient

    live = {"n": 0}
    original_call = OpenAiCompatClient._call_sync

    def counting_call(self, system_prompt, user_prompt, temperature):
        live["n"] += 1
        log.info(
            "  LLM 真实调用 #%d（prompt %d 字符，其余命中缓存不计）",
            live["n"],
            len(user_prompt),
        )
        return original_call(self, system_prompt, user_prompt, temperature)

    OpenAiCompatClient._call_sync = counting_call

    async def _run() -> None:
        from qwenpaw_data_auth import Principal

        from profiling.db import init_probe_db
        from profiling.repositories import staging_repo
        from profiling.services import orchestrator
        from profiling.stages import def_by_stage
        from semantic_config.config import get_settings
        from semantic_config.db import init_db
        from semantic_config.repositories import datasource_repo as ds_repo

        settings = get_settings()
        originals = settings.db_url
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        settings.db_url = "sqlite+aiosqlite:///" + tmp.name
        try:
            # ==============================================================
            # ⓪ 载入录制数据 + 指纹守卫 + 登记数据源
            # ==============================================================
            log.info(
                "═══ STEP 0 载入：临时库 %s / out_dir %s ═══",
                tmp.name,
                _OUT_ROOT / _DS_ID,
            )
            _wipe_context_snapshots(_OUT_ROOT / _DS_ID)
            _restore_fixture(_OUT_ROOT / _DS_ID)
            # 指纹前置：provider 将用同一份 config 装配 —— 指纹不等于录制值就会去连
            # 占位 host，这里先断言，把「意外发远程查询」在跑之前拦下
            from profiling.algo.inputs import QueryLogInput, TableInput

            replay_cfg = algo_config.AlgoConfig.build(
                datasource_id=_DS_ID,
                datasource_type="POSTGRESQL",
                datasource_connection=_CONNECTION,
            )
            for ctx, expected_fp in (
                ("source_table", TableInput.fingerprint(replay_cfg)),
                ("query_log_raw", QueryLogInput.fingerprint(replay_cfg)),
            ):
                manifest = json.loads(
                    (_FIXTURE / ctx / "_meta" / "snapshot.json").read_text()
                )
                assert manifest["fingerprint"] == expected_fp, (
                    f"{ctx} 指纹与录制不一致（会触发远程拉取）: 录制 {manifest['fingerprint']} != 本次 {expected_fp}"
                )
                log.info(
                    "  ✓ %s 指纹与录制一致（录制于 %s，%s）",
                    ctx,
                    manifest["recorded_at"][:19],
                    manifest["rows"]
                    if ctx == "query_log_raw"
                    else f"{len(manifest['keys'])} 个帧",
                )

            await init_db()
            await init_probe_db()
            from semantic_config.engine import get_session_factory

            conn = get_session_factory()()
            admin = Principal(
                user_id="u1",
                tenant_id="t1",
                workspace_ids=["w1"],
                active_workspace_id="w1",
                role="tenant_admin",
            )
            await ds_repo.insert(
                conn,
                _DS_ID,
                "E2E holo (offline replay)",
                "POSTGRESQL",
                json.dumps(_CONNECTION),
                tenant_id=admin.tenant_id,
                workspace_id=admin.active_workspace_id,
                created_by=admin.user_id,
            )
            await conn.commit()
            source_tables = _source_tables()
            comments = _table_comments()
            log.info(
                "  数据源已登记：录制表面 %d 张表（%d 张带注释），样例 %s …",
                len(source_tables),
                sum(1 for v in comments.values() if v),
                sorted(source_tables)[:5],
            )

            # ==============================================================
            # ① BIZ_DOMAIN 探查（真实 AlgoProvider → DOMAIN 任务 → staging）
            # ==============================================================
            log.info(
                "═══ STEP 1 BIZ_DOMAIN 探查（prior 为空，录制的 Hologres 日志作证据）═══"
            )
            await orchestrator.submit(conn, _DS_ID, "BIZ_DOMAIN", admin, mode="create")
            await _drain(orchestrator)
            await conn.commit()
            task = await orchestrator.get_task(conn, _DS_ID, "BIZ_DOMAIN", admin)
            assert task.status == "SUCCESS", (
                f"BIZ_DOMAIN 探查失败: status={task.status} message={task.message}"
            )
            sdef = def_by_stage("BIZ_DOMAIN")
            rows = [
                dict(r)
                for r in await staging_repo.list_all(
                    conn,
                    sdef.table,
                    _DS_ID,
                    admin.tenant_id,
                    admin.active_workspace_id,
                )
            ]
            assert rows, "探查没有产出任何业务域"
            log.info("── BIZ_DOMAIN 探查完成：staging %d 行", len(rows))

            # -- 日志证据（离线回读 provider 落盘的解析结果）--
            from profiling.algo import AlgoContexts

            logs = await AlgoContexts.build(replay_cfg, offline=True).query_log.load()
            joins = logs.of("join")
            edges = [
                (str(l), str(r))
                for l, r in zip(joins["left_table"], joins["right_table"])
            ]
            counts = {
                k: len(logs.of(k))
                for k in ("join", "sql_template", "dimension_usage", "metric_expr")
            }
            assert len(edges) > 0, "录制日志含 JOIN，却没有 join 证据"
            log.info("  日志证据: %s；join 边: %s", counts, edges)

            # -- 结构断言：状态 / 成员合法 / 同 cluster 同域 --
            domain_of: dict[str, str] = {}
            for r in rows:
                assert r["probe_status"] == "PENDING" and r["source_type"] == "ALGO", (
                    f"首探行状态异常: {r['domain_name']} {r['probe_status']}/{r['source_type']}"
                )
                members = _member_tables_of(r)
                unknown = members - source_tables
                assert not unknown, f"域 {r['domain_name']} 含未知成员表: {unknown}"
                for t in members:
                    assert t not in domain_of, (
                        f"表 {t} 被归入两个域: {domain_of[t]} / {r['domain_name']}"
                    )
                    domain_of[t] = r["domain_name"]
            split = [
                (l, r, domain_of[l], domain_of[r])
                for l, r in edges
                if l in domain_of and r in domain_of and domain_of[l] != domain_of[r]
            ]
            assert not split, f"同 cluster 的表被划进了不同域: {split}"
            covered = set(domain_of)
            uncovered = sorted(source_tables - covered)
            coverage = len(covered) / len(source_tables)
            log.info(
                "  ✓ join 边两端均同域（%d 条）；表覆盖率 %d/%d = %.0f%%",
                len(edges),
                len(covered),
                len(source_tables),
                coverage * 100,
            )

            # ==============================================================
            # 呈现：逐域打印 + 落盘 markdown / json
            # ==============================================================
            rows.sort(key=lambda r: (-len(_member_tables_of(r)), r["domain_name"]))
            result = []
            md = [
                f"# Hologres 离线回放 · DOMAIN 探查结果（{_DS_ID}）",
                "",
                f"- 表面：{len(source_tables)} 张（录制于 {_FIXTURE.name}）；查询日志 1000 条 → 证据 {counts}",
                f"- 域：{len(rows)} 个；表覆盖 {len(covered)}/{len(source_tables)}（{coverage:.0%}）；LLM 真实调用 {live['n']} 次",
                f"- join 边（同域校验通过）：{edges}",
                "",
                "| # | domain | 成员数 | description |",
                "|---|---|---|---|",
            ]
            for i, r in enumerate(rows, 1):
                members = sorted(_member_tables_of(r))
                result.append(
                    {
                        "domain_name": r["domain_name"],
                        "description": r["description"],
                        "members": members,
                    }
                )
                md.append(
                    f"| {i} | `{r['domain_name']}` | {len(members)} | {(r['description'] or '').replace('|', '/')} |"
                )
                log.info(
                    "  域 %-28s 成员 %3d 张 | %s",
                    r["domain_name"],
                    len(members),
                    (r["description"] or "")[:60],
                )
            md += ["", "## 各域成员表", ""]
            for i, item in enumerate(result, 1):
                md += [
                    f"### {i}. `{item['domain_name']}`（{len(item['members'])} 张）",
                    "",
                ]
                md += [
                    f"- `{t}`" + (f" — {comments[t]}" if comments.get(t) else "")
                    for t in item["members"]
                ]
                md.append("")
            md += [f"## 未归域的表（{len(uncovered)} 张）", ""] + [
                f"- `{t}`" for t in uncovered
            ]
            out_dir = _OUT_ROOT / _DS_ID
            (out_dir / "domain_result.md").write_text("\n".join(md), encoding="utf-8")
            (out_dir / "domain_result.json").write_text(
                json.dumps(
                    {
                        "domains": result,
                        "uncovered": uncovered,
                        "join_edges": edges,
                        "evidence": counts,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            log.info(
                "E2E(holo·DOMAIN) ✓ %d 个域，覆盖 %.0f%%，LLM 真实调用 %d 次 → 结果 %s",
                len(rows),
                coverage * 100,
                live["n"],
                out_dir / "domain_result.md",
            )

            # ==============================================================
            # ② BIZ_DOMAIN confirm 全部 → complete（promote → domain）
            # ==============================================================
            log.info("═══ STEP 2 BIZ_DOMAIN confirm 全部 → complete → promote ═══")
            from sqlalchemy import text

            from profiling.services import row_service, stage_service

            async def scalar(sql: str) -> int:
                res = await conn.execute(text(sql))
                val = res.scalar()
                return val if val is not None else 0

            await row_service.batch_confirm(
                conn, sdef, _DS_ID, [r["id"] for r in rows], admin
            )
            await conn.commit()
            done = await stage_service.complete(conn, _DS_ID, "BIZ_DOMAIN", admin)
            await conn.commit()
            assert done["status"] == "COMPLETED", f"BIZ_DOMAIN complete 失败: {done}"
            n_formal = await scalar("SELECT COUNT(*) FROM domain WHERE is_deleted=0")
            assert n_formal == len(rows), (
                f"正式 domain {n_formal} 行 != 确认的 {len(rows)} 行"
            )
            log.info(
                "  ✓ %d 个域全部确认并 promote → 正式 domain %d 行", len(rows), n_formal
            )

            # ==============================================================
            # ③ DATASET 抽取：直通（白名单∩已归域）+ 日志式（模板 ≥3 次）
            # ==============================================================
            log.info("═══ STEP 3 DATASET 抽取（直通 view%%/dataset%% + 日志式模板）═══")
            await orchestrator.submit(conn, _DS_ID, "DATASET", admin, mode="create")
            await _drain(orchestrator)
            await conn.commit()
            task = await orchestrator.get_task(conn, _DS_ID, "DATASET", admin)
            assert task.status == "SUCCESS", (
                f"DATASET 探查失败: status={task.status} message={task.message}"
            )
            ds_sdef = def_by_stage("DATASET")
            ds_rows = [
                dict(r)
                for r in await staging_repo.list_all(
                    conn,
                    ds_sdef.table,
                    _DS_ID,
                    admin.tenant_id,
                    admin.active_workspace_id,
                )
            ]
            assert ds_rows, "DATASET 探查没有产出任何数据集"
            await row_service.batch_confirm(
                conn, ds_sdef, _DS_ID, [r["id"] for r in ds_rows], admin
            )
            await conn.commit()
            done = await stage_service.complete(conn, _DS_ID, "DATASET", admin)
            await conn.commit()
            assert done["status"] == "COMPLETED"
            assert await scalar("SELECT COUNT(*) FROM dataset WHERE is_deleted=0") == 0

            await orchestrator.submit(
                conn, _DS_ID, "DATASET_FIELDS", admin, mode="create"
            )
            await _drain(orchestrator)
            await conn.commit()
            field_task = await orchestrator.get_task(
                conn, _DS_ID, "DATASET_FIELDS", admin
            )
            assert field_task.status == "SUCCESS", (
                f"DATASET_FIELDS 探查失败: {field_task.message}"
            )
            field_rows = [
                dict(r)
                for r in await staging_repo.list_all(
                    conn,
                    "staging_dataset_field",
                    _DS_ID,
                    admin.tenant_id,
                    admin.active_workspace_id,
                )
            ]
            assert field_rows, "DATASET_FIELDS 探查没有产出 dataset_field"
            fields_by_ds: dict[str, list[dict]] = {}
            for f in field_rows:
                fields_by_ds.setdefault(str(f["dataset_name"]), []).append(f)
            log.info(
                "── DATASET 探查完成：staging_dataset %d 行，staging_dataset_field %d 行",
                len(ds_rows),
                len(field_rows),
            )

            # -- 直通集合 == AlgoConfig 默认白名单（Dataset 会补齐缺失的 Domain 归属）--
            # v2.1 单指针：source 是 SQL → 日志式；否则是直通的物理表名。**不按名字判**：
            # LLM 给模板起的名字可能恰好等于某张物理表（例如 order_status）
            def is_sql(v) -> bool:
                return "select" in ("" if v is None else str(v)).lower()

            whitelist = _like_matches(
                source_tables, replay_cfg.dataset_derived_from_tables_include_patterns
            )
            expected_pt = whitelist
            passthrough = [r for r in ds_rows if not is_sql(r.get("source"))]
            derived = [r for r in ds_rows if is_sql(r.get("source"))]
            pt_names = {str(r["dataset_name"]) for r in passthrough}
            assert pt_names == expected_pt, (
                f"直通集合不符：多出 {sorted(pt_names - expected_pt)}，缺 {sorted(expected_pt - pt_names)}"
            )
            collisions = {str(r["dataset_name"]) for r in derived} & source_tables
            if collisions:
                log.warning(
                    "  日志式 dataset 与物理表重名（LLM 起名碰撞，同源会撞 (datasource_id, dataset_name) 键）: %s",
                    sorted(collisions),
                )
            log.info(
                "  直通 %d 张（白名单命中 %d，其中已归域 %d）+ 日志式 %d 个",
                len(pt_names),
                len(whitelist),
                len(expected_pt),
                len(derived),
            )

            # -- 逐数据集核对 dataset 行 + 字段 --
            def norm(v) -> str:
                return "" if v is None or str(v) in ("nan", "None") else str(v)

            # 录制列面经算法库自己的 store 回读（文件名由 store 的 _safe() 决定，
            # 不能从表名反推 —— 含 '#' 的表名会改写），与 provider 看到的完全一致
            src_snap = await AlgoContexts.build(
                replay_cfg, offline=True
            ).source_table.load()

            def recorded_columns(table: str) -> dict[str, dict] | None:
                frame = src_snap.column_meta.get(table)
                if frame is None or len(frame) == 0:
                    return None
                return {
                    str(x.column_name): {
                        "data_type": norm(x.data_type),
                        "is_primary": norm(x.is_primary),
                        "is_nullable": norm(x.is_nullable),
                        "comment": norm(x.column_comment),
                    }
                    for x in frame.itertuples(index=False)
                }

            field_mismatch: list[str] = []
            schema_prefixes: set[str] = set()
            for r in ds_rows:
                name = str(r["dataset_name"])
                assert r["probe_status"] == "PENDING" and r["source_type"] == "ALGO", (
                    f"dataset 行状态异常: {name} {r['probe_status']}/{r['source_type']}"
                )
                assert norm(r.get("source")), f"dataset {name} 缺 source 单指针"
                fields = fields_by_ds.get(name, [])
                if name in pt_names:
                    # v2.1：直通 source 写物理全名（``schema.table``，P3 两段全名同形态），末段即表名
                    src_full = norm(r["source"])
                    assert src_full.rsplit(".", 1)[-1] == name, (
                        f"直通 dataset {name} 的 source 末段应为表名，实得 {r['source']!r}"
                    )
                    schema_prefixes.add(src_full[: -len(name)].rstrip("."))
                    assert norm(r.get("domain_name")) == domain_of[name], (
                        f"直通 dataset {name} 域归属 {r.get('domain_name')!r} != DOMAIN 阶段 {domain_of[name]!r}"
                    )
                    recorded = recorded_columns(name)
                    if recorded is None:
                        assert not fields, (
                            f"直通 {name} 无录制列面却产出了 {len(fields)} 个字段"
                        )
                        continue
                    got = {str(f["field_name"]): f for f in fields}
                    if set(got) != set(recorded):
                        field_mismatch.append(
                            f"{name}: 字段集合 多出 {sorted(set(got) - set(recorded))} 缺 {sorted(set(recorded) - set(got))}"
                        )
                        continue
                    for col, rec in recorded.items():
                        f = got[col]
                        for key, mine in (
                            ("data_type", norm(f["data_type"])),
                            ("is_primary", norm(f["is_primary"])),
                            ("is_nullable", norm(f["is_nullable"])),
                            ("comment", norm(f["description"])),
                        ):
                            if mine != rec[key]:
                                field_mismatch.append(
                                    f"{name}.{col}: {key} {mine!r} != 录制 {rec[key]!r}"
                                )
                else:
                    assert "select" in norm(r["source"]).lower(), (
                        f"日志式 dataset {name} 的 source 不是 SQL: {r['source']!r}"
                    )
                    assert norm(r.get("description")), f"日志式 dataset {name} 缺描述"
                    assert fields, f"日志式 dataset {name} 没有任何字段（投影继承失败）"
                    for f in fields:
                        assert norm(f["field_name"]), (
                            f"日志式 dataset {name} 有空字段名"
                        )
            assert not field_mismatch, "直通字段与录制列面不一致:\n  " + "\n  ".join(
                field_mismatch[:20]
            )
            n_pt_fields = sum(len(fields_by_ds.get(n, [])) for n in pt_names)
            n_dv_fields = sum(
                len(fields_by_ds.get(str(r["dataset_name"]), [])) for r in derived
            )
            origins = sorted({norm(f.get("origin")) for f in field_rows})
            log.info(
                "  ✓ 直通 %d 张表（source 为物理全名，schema 前缀 %s）%d 个字段与录制列面逐项一致；日志式 %d 个 dataset 共 %d 个字段；field.origin 取值 %s",
                len(pt_names),
                sorted(schema_prefixes),
                n_pt_fields,
                len(derived),
                n_dv_fields,
                origins,
            )
            for r in derived:
                log.info(
                    "    日志式 %s（%d 字段）: %.90s…",
                    r["dataset_name"],
                    len(fields_by_ds.get(str(r["dataset_name"]), [])),
                    norm(r["source"]).replace("\n", " "),
                )

            # -- 呈现：落盘 markdown / json --
            ds_result = []
            md = [
                f"# Hologres 离线回放 · DATASET 抽取结果（{_DS_ID}）",
                "",
                f"- 直通：{len(pt_names)} 张（白名单 view%/dataset% 命中 {len(whitelist)}，已归域 {len(expected_pt)}），字段 {n_pt_fields} 个，与录制列面逐项一致",
                f"- 日志式：{len(derived)} 个（1000 条日志中重复 ≥3 次的 SQL 模板），字段 {n_dv_fields} 个",
                f"- LLM 真实调用 {live['n']} 次",
                "",
                "| # | dataset | 来源 | domain | 字段数 | description |",
                "|---|---|---|---|---|---|",
            ]
            for i, r in enumerate(
                sorted(
                    ds_rows,
                    key=lambda x: (
                        x["dataset_name"] not in pt_names,
                        x["dataset_name"],
                    ),
                ),
                1,
            ):
                name = str(r["dataset_name"])
                fl = fields_by_ds.get(name, [])
                kind = "直通" if name in pt_names else "日志式"
                ds_result.append(
                    {
                        "dataset_name": name,
                        "kind": kind,
                        "domain_name": r.get("domain_name"),
                        "description": r.get("description"),
                        "source": r.get("source"),
                        "fields": [
                            {
                                "field_name": f["field_name"],
                                "data_type": f["data_type"],
                                "is_primary": f["is_primary"],
                                "is_nullable": f["is_nullable"],
                                "description": f["description"],
                                "origin": f.get("origin"),
                            }
                            for f in fl
                        ],
                    }
                )
                md.append(
                    f"| {i} | `{name}` | {kind} | {r.get('domain_name') or ''} | {len(fl)} | {norm(r.get('description')).replace('|', '/')[:80]} |"
                )
            md += ["", "## 各数据集字段", ""]
            for item in ds_result:
                md += [
                    f"### `{item['dataset_name']}`（{item['kind']}，{len(item['fields'])} 字段）",
                    "",
                ]
                if item["kind"] == "日志式":
                    md += ["```sql", norm(item["source"]), "```", ""]
                md += [
                    "| field | type | pk | nullable | origin | description |",
                    "|---|---|---|---|---|---|",
                ]
                md += [
                    f"| `{f['field_name']}` | {norm(f['data_type'])} | {norm(f['is_primary'])} | {norm(f['is_nullable'])} | {norm(f['origin'])} | {norm(f['description']).replace('|', '/')[:60]} |"
                    for f in item["fields"]
                ]
                md.append("")
            (out_dir / "dataset_result.md").write_text("\n".join(md), encoding="utf-8")
            (out_dir / "dataset_result.json").write_text(
                json.dumps({"datasets": ds_result}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            # ==============================================================
            # ④ DATASET_FIELDS confirm 全部 → complete → promote：dataset + dataset_field
            # ==============================================================
            log.info(
                "═══ STEP 4 DATASET_FIELDS confirm 全部 → Dataset + dataset_field promote ═══"
            )
            field_sdef = def_by_stage("DATASET_FIELDS")
            await row_service.batch_confirm(
                conn, field_sdef, _DS_ID, [r["id"] for r in field_rows], admin
            )
            await conn.commit()
            done = await stage_service.complete(conn, _DS_ID, "DATASET_FIELDS", admin)
            await conn.commit()
            assert done["status"] == "COMPLETED", (
                f"DATASET_FIELDS complete 失败: {done}"
            )
            n_ds = await scalar("SELECT COUNT(*) FROM dataset WHERE is_deleted=0")
            # 正式字段表是 dataset_column_meta（tables.py 里 dataset_field 只是旧名别名）
            n_fields = await scalar(
                "SELECT COUNT(*) FROM dataset_column_meta WHERE is_deleted=0"
            )
            assert n_ds == len(ds_rows), (
                f"正式 dataset {n_ds} 行 != staging 确认的 {len(ds_rows)} 行"
            )
            assert n_fields == len(field_rows), (
                f"正式 dataset_column_meta {n_fields} 行 != staging {len(field_rows)} 行"
            )
            log.info(
                "E2E(holo·DOMAIN+DATASET) ✓ 域 %d / dataset %d（直通 %d + 日志式 %d）/ 字段 %d 全部 promote；LLM 真实调用 %d 次 → 结果 %s",
                n_formal,
                n_ds,
                len(pt_names),
                len(derived),
                n_fields,
                live["n"],
                out_dir / "dataset_result.md",
            )
            await conn.close()
        finally:
            settings.db_url = originals
            if os.environ.get("E2E_KEEP_DB") == "1":
                log.info("E2E_KEEP_DB=1：临时库保留于 %s", tmp.name)
            else:
                Path(tmp.name).unlink(missing_ok=True)

    try:
        asyncio.run(_run())
    finally:
        OpenAiCompatClient._call_sync = original_call
        for key, value in saved_env.items():
            os.environ.pop(key, None) if value is None else os.environ.__setitem__(
                key, value
            )
        algo_config._baseline.cache_clear()
