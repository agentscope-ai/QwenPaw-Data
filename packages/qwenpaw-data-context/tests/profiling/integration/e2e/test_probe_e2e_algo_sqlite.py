"""后端全链路 E2E：真实 AlgoProvider + spider sqlite 本地库 + 七阶段 + prior 闭环。

这是「跑完它 ≈ 后端主要任务操作都测过」的那条测试。与既有测试面的分工：

- ``tests/profiling/integration/algo/test_spider_sqlite.py``   只测算法库层（AlgoProfiler 直调），
  不经过 provider / staging / 状态机；
- ``tests/profiling/integration/test_probe_full_flow.py``      测编排层七阶段，但用 StubProvider
  顶替，真算法桥 ``algo_provider.py`` 一行不走；
- **本文件**                              不换 provider —— 生产默认的
  ``profiling.algo_provider:AlgoProvider`` 全链路真跑：
  ``submit → _load_facts → AlgoContexts.build(facts=…) → 四任务 →
  map_* 投影 → DbProbeSink → staging → 行操作 → 重跑(prior) →
  complete → promote → 正式表``。

主链路十步（单个测试函数，顺序即依赖）：

  ① BIZ_DOMAIN 首次探查（prior 为空，纯表名归域）
  ② 行操作：confirm 一个域（并编辑 description）、skip 一个域（= 删除）、
     人工新增一个 HUMAN 空成员域、清空其余草稿的描述
  ③a 完善探查（refine）：PENDING（含 HUMAN）全部完善；有成员草稿成员不动；仅 CONFIRMED 冻结
  ③b 重新探查（create）：删两个草稿后重分域 —— 锁定行 id/状态/成员完全不变，
     新候选可复用锁定域名但不碰锁定成员表，未锁定表覆盖率 ≥ 80%，被删域的表回收
  ③c 删全部草稿 + HUMAN default → 重新探查：未确认 HUMAN 可被替换，仅 CONFIRMED 冻结
     （「全部归入 default」在成员冻结语义下不支持，不断言）
  ④ confirm 全部 → complete → promote → 正式表 domain
  ⑤ DATASET 直通（env 白名单 ``["%"]``）→ confirm → complete；
     再探查并确认 DATASET_FIELDS → 正式 dataset / dataset_column_meta
  ⑥ DIMENSION_CALIBER → confirm → complete（口径阶段不 promote）
  ⑦ DIMENSION_MANAGE（DICT 维度侧）→ complete → dimension + 绑定 promote
  ⑧ METRIC_CALIBER → confirm → complete
  ⑨ METRIC_MANAGE（DICT 指标侧）→ complete → metric promote
  ⑩ 总态 OVERALL_COMPLETED + 正式表核数 + LLM 真实调用数汇总
  ⑪ 官方导出器 export_workbook_from_storage → 12-sheet workbook：与模板同形校验 + 逐 sheet 与正式表对账

前提与费用：

- 需要 ``data/Spider2`` 本地库与 .env 的 ``OPENAI_API_KEY``，缺任一则 skip；
- LLM 是真实客户端，响应缓存落 ``data/profile/e2e/``（.gitignore 已覆盖）：
  参数不变的重跑近乎免费，首跑约 15~25 次真实调用；
- 数据库走 tmp SQLite 隔离（同时清 db_url，防止 .env 共享库吞掉覆盖）。

运行（日志全量输出到终端）::

    cd packages/qwenpaw-data-context
    uv run --no-sync --with pytest python -m pytest \
        tests/profiling/integration/e2e/test_probe_e2e_algo_sqlite.py -s --log-cli-level=INFO
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

log = logging.getLogger("test_probe_e2e")

_PKG = Path(__file__).resolve().parents[4]  # packages/qwenpaw-data-context
_REPO = Path(__file__).resolve().parents[6]  # 仓库根
_DB = "IPL"  # 带 2 条 gold SQL：join / 用法证据可点亮
_DB_PATH = (
    _REPO
    / "data"
    / "Spider2"
    / "spider2-lite"
    / "resource"
    / "databases"
    / "spider2-localdb"
    / f"{_DB}.sqlite"
)
_HOME_ROOT = Path(tempfile.gettempdir()) / "qwenpaw-data-profiling-e2e-home"
_OUT_ROOT = _HOME_ROOT / "t1" / "w1" / ".cm" / "profile"
_DS_ID = f"e2e-{_DB}"  # 固定 → 跨运行共享 LLM 缓存
_SPIDER_LITE = _REPO / "data" / "Spider2" / "spider2-lite"
_GOLD_INDEX = _SPIDER_LITE / "spider2-lite.jsonl"  # 每行 {"instance_id", "db", ...}
_GOLD_DIR = _SPIDER_LITE / "evaluation_suite" / "gold" / "sql"

#: AlgoProvider 注入数据源身份、模式和按租户隔离的产物目录，其余参数走
#: env / 默认值 —— spider 前提（全表直通、英文产出、枚举、自建执行器）
#: 只能经 env 注入。这也是本测试顺带钉住的事实：想给某数据源定制算法
#: 参数，目前唯一入口就是部署环境变量。
_ENV_OVERRIDES = {
    "EXECUTE_SQL_PATH": "",  # 自建 sqlite 执行器
    "QWENPAW_DATA_HOME": str(_HOME_ROOT),  # 在线 Provider 的稳定缓存目录
    "STORE_OUT_DIR": str(_OUT_ROOT),  # 离线回读同一目录
    "LLM_OUTPUT_LANGUAGE": "en",  # spider 全英文
    "DATASET_DERIVED_FROM_TABLES_INCLUDE_PATTERNS": '["%"]',  # 全表直通
    "DATASET_PARSE_FROM_LOGS_ENABLED": "true",  # gold SQL 也作日志式 dataset 来源
    "INPUT_TABLE_ENUMS_ENABLED": "true",  # 覆盖 value_example 链路
    # -- 查询日志 = 评测集 gold SQL（文件来源，零 SQL 执行）--
    "INPUT_QUERY_LOG_DATASOURCE_TYPE": "sql_files",
    "INPUT_QUERY_LOG_PATH": str(_GOLD_INDEX),
    "INPUT_QUERY_LOG_DIR": str(_GOLD_DIR),
    "INPUT_QUERY_LOG_DBNAME": _DB,
    "PARSER_DIALECT": "sqlite",
    "PARSER_TEMPLATE_MIN_COUNT": "1",  # 1~3 条 gold，不可能重复 10 次
    "PARSER_TEMPLATE_MIN_COLUMNS": "3",  # gold 是答题查询，投影窄
}

_STAGE_ORDER = (
    "BIZ_DOMAIN",
    "DATASET",
    "DIMENSION_CALIBER",
    "DIMENSION_MANAGE",
    "METRIC_CALIBER",
    "METRIC_MANAGE",
)


# ===========================================================================
# 工具
# ===========================================================================


def _source_tables() -> set[str]:
    """当前 E2E 库（``_DB``）的真实表/视图名（与输入层同口径：排除 sqlite_ 系统表）。"""
    with sqlite3.connect(_DB_PATH) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    return {r[0] for r in rows}


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
    """清掉上一轮的 context 快照（本地 parquet 会被当作 prior 读回，破坏
    「首次探查 prior 为空」的前提）；只留 llm/ —— 那是响应缓存，钱。"""
    if not out_dir.is_dir():
        return
    for child in out_dir.iterdir():
        if child.is_dir() and child.name != "llm":
            shutil.rmtree(child)
            log.info("  已清理上一轮快照目录 %s", child.name)


async def _drain(orchestrator) -> None:
    """等待所有后台探查任务结束（真 LLM，首跑分钟级）。"""
    for _ in range(10_000):
        tasks = list(orchestrator._REGISTRY.values())
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)


# ===========================================================================
# 主链路
# ===========================================================================


def test_e2e_full_backend_chain_sqlite():
    if not _DB_PATH.is_file():
        pytest.skip(f"Spider2 本地库缺失: {_DB_PATH}")

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
        from profiling.models.probe import OVERALL_COMPLETED
        from profiling.repositories import staging_repo
        from profiling.services import orchestrator, row_service, stage_service
        from profiling.stages import def_by_stage
        from semantic_config.config import get_settings
        from semantic_config.db import init_db
        from semantic_config.repositories import datasource_repo as ds_repo
        from sqlalchemy import text

        settings = get_settings()
        originals = settings.db_url
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        settings.db_url = "sqlite+aiosqlite:///" + tmp.name
        try:
            log.info("═══ STEP 0 环境：临时库 %s / out_dir %s ═══", tmp.name, _OUT_ROOT)
            _wipe_context_snapshots(_OUT_ROOT / _DS_ID)
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
                "E2E",
                "SQLITE",
                json.dumps({"db_path": str(_DB_PATH)}),
                tenant_id=admin.tenant_id,
                workspace_id=admin.active_workspace_id,
                created_by=admin.user_id,
            )
            await conn.commit()
            source_tables = _source_tables()
            log.info(
                "  数据源已登记：%d 张源表 %s",
                len(source_tables),
                sorted(source_tables),
            )

            async def scalar(sql: str) -> int:
                res = await conn.execute(text(sql))
                val = res.scalar()
                return val if val is not None else 0

            async def probe(stage: str, *, mode: str = "create") -> list[dict]:
                """提交探查 / 重新探查 / 完善探查 → 等结束 → 校验 SUCCESS → 返回 staging 行。"""
                label = {
                    "create": "探查",
                    "rerun": "重新探查",
                    "refine": "完善探查",
                }.get(mode, mode)
                log.info("── %s %s 提交…", stage, label)
                await orchestrator.submit(conn, _DS_ID, stage, admin, mode=mode)
                await _drain(orchestrator)
                await conn.commit()
                task = await orchestrator.get_task(conn, _DS_ID, stage, admin)
                assert task.status == "SUCCESS", (
                    f"{stage} {label}失败: status={task.status} message={task.message}"
                )
                sdef = def_by_stage(stage)
                rows = await staging_repo.list_all(
                    conn,
                    sdef.table,
                    _DS_ID,
                    admin.tenant_id,
                    admin.active_workspace_id,
                )
                log.info("── %s %s 完成：staging %d 行", stage, label, len(rows))
                return [dict(r) for r in rows]

            async def confirm_all_and_complete(stage: str) -> None:
                """候选态阶段 confirm 全部 PENDING 行，然后 complete（promote）。"""
                sdef = def_by_stage(stage)
                if sdef.has_rows:
                    rows = await staging_repo.list_all(
                        conn,
                        sdef.table,
                        _DS_ID,
                        admin.tenant_id,
                        admin.active_workspace_id,
                    )
                    pending = [r["id"] for r in rows if r["probe_status"] == "PENDING"]
                    if pending:
                        await row_service.batch_confirm(
                            conn, sdef, _DS_ID, pending, admin
                        )
                        await conn.commit()
                    log.info(
                        "── %s 批量确定 %d 行（共 %d 行）",
                        stage,
                        len(pending),
                        len(rows),
                    )
                result = await stage_service.complete(conn, _DS_ID, stage, admin)
                await conn.commit()
                assert result["status"] == "COMPLETED", (
                    f"{stage} complete 失败: {result}"
                )
                log.info(
                    "── %s COMPLETED（promoted=%s）", stage, result.get("promoted", "-")
                )

            # ==============================================================
            # ① BIZ_DOMAIN 首次探查
            # ==============================================================
            async def log_evidence(kind: str):
                """算法层落盘的日志证据，按生产路径同一份 config 离线回读
                （offline=True 不建任何输入来源，只认指纹匹配的本地缓存）。"""
                from profiling.algo import AlgoContexts

                cfg = algo_config.AlgoConfig.build(
                    datasource_id=_DS_ID,
                    datasource_type="SQLITE",
                    datasource_connection={"db_path": str(_DB_PATH)},
                )
                logs = await AlgoContexts.build(cfg, offline=True).query_log.load()
                return logs.of(kind)

            log.info(
                "═══ STEP 1 BIZ_DOMAIN 首次探查（prior 应为空，gold SQL 作日志）═══"
            )
            rows = await probe("BIZ_DOMAIN")
            assert rows, "首次探查没有产出任何业务域"
            joins = await log_evidence("join")
            assert len(joins) > 0, f"{_DB} 的 gold SQL 含 JOIN，却没有 join 证据"
            edges = [
                (str(l), str(r))
                for l, r in zip(joins["left_table"], joins["right_table"])
            ]
            log.info("  join 证据 %d 条边: %s", len(edges), edges[:6])
            domain_of = {
                t: r["domain_name"] for r in rows for t in _member_tables_of(r)
            }
            split = [
                (l, r, domain_of[l], domain_of[r])
                for l, r in edges
                if l in domain_of and r in domain_of and domain_of[l] != domain_of[r]
            ]
            assert not split, f"同 cluster 的表被划进了不同域: {split}"
            log.info("  ✓ join 边两端均同域（%d 条边全部一致）", len(edges))
            for r in rows:
                assert r["probe_status"] == "PENDING" and r["source_type"] == "ALGO", (
                    f"首探行状态异常: {r['domain_name']} "
                    f"{r['probe_status']}/{r['source_type']}"
                )
                members = _member_tables_of(r)
                unknown = members - source_tables
                assert not unknown, f"域 {r['domain_name']} 含未知成员表: {unknown}"
                log.info(
                    "  域 %-24s 成员 %d 张: %s",
                    r["domain_name"],
                    len(members),
                    sorted(members),
                )

            # ==============================================================
            # ② 行操作：confirm+编辑 / skip / 人工新增 / 清空草稿描述
            #    选择全部确定性（按 id 排序），不用随机 —— prompt 稳定才能命中缓存
            # ==============================================================
            log.info(
                "═══ STEP 2 行操作（confirm+edit / skip / HUMAN 新增 / 清空草稿描述）═══"
            )
            sdef = def_by_stage("BIZ_DOMAIN")
            rows = sorted(rows, key=lambda r: r["id"])

            async def staging_rows() -> list[dict]:
                found = await staging_repo.list_all(
                    conn, sdef.table, _DS_ID, admin.tenant_id, admin.active_workspace_id
                )
                return [dict(r) for r in found]

            async def add_human(name: str, desc: str) -> int:
                await row_service.add_row(
                    conn,
                    sdef,
                    _DS_ID,
                    {"domain_name": name, "description": desc},
                    admin,
                )
                await conn.commit()
                return next(
                    r["id"]
                    for r in await staging_rows()
                    if r["domain_name"] == name and r["source_type"] == "HUMAN"
                )

            locked_row = rows[0]
            locked_id, locked_name = locked_row["id"], locked_row["domain_name"]
            locked_members = _member_tables_of(locked_row)
            await row_service.update_row(
                conn,
                sdef,
                _DS_ID,
                locked_id,
                {"description": "E2E-LOCKED-DESCRIPTION"},
                admin,
            )
            await row_service.batch_confirm(conn, sdef, _DS_ID, [locked_id], admin)
            log.info(
                "  已确认并修改描述: %s（成员 %s）", locked_name, sorted(locked_members)
            )

            skipped_id = skipped_name = None
            if len(rows) >= 2:
                skipped_id, skipped_name = rows[1]["id"], rows[1]["domain_name"]
                await row_service.batch_skip(conn, sdef, _DS_ID, [skipped_id], admin)
                log.info("  已 skip（= 删除语义）: %s", skipped_name)
            else:
                log.warning("  首探只有 1 个域，跳过 skip 场景")

            human_name = "e2e_manual_domain"
            human_id = await add_human(
                human_name, "human-added row, must survive re-probe"
            )
            log.info(
                "  已人工新增（source_type=HUMAN, id=%d）: %s", human_id, human_name
            )

            # 场景 1 的前置：清空其余草稿（PENDING+ALGO）的描述
            draft_rows = [r for r in rows if r["id"] not in {locked_id, skipped_id}]
            for r in draft_rows:
                await row_service.update_row(
                    conn, sdef, _DS_ID, r["id"], {"description": ""}, admin
                )
            await conn.commit()
            draft_ids_before = {r["id"] for r in draft_rows}
            draft_member_sets = sorted(sorted(_member_tables_of(r)) for r in draft_rows)
            # 仅 CONFIRMED 冻结；HUMAN PENDING 是可完善 / 可替换草稿
            locked_ids = {locked_id}
            pending_draft_count = len(draft_rows) + 1  # ALGO 草稿 + HUMAN
            log.info(
                "  已清空 %d 个草稿的描述: %s；HUMAN 草稿: %s",
                len(draft_rows),
                [r["domain_name"] for r in draft_rows],
                human_name,
            )

            def check_locked_frozen(by_id: dict[int, dict], step: str) -> None:
                """仅 CONFIRMED 锁定行原地保留：id / 名称 / description / 状态 / 成员不变。"""
                assert locked_id in by_id, f"{step}: 确认域行 id={locked_id} 丢失"
                lr = by_id[locked_id]
                assert lr["domain_name"] == locked_name, (
                    f"{step}: 确认域被改名 {lr['domain_name']}"
                )
                assert lr["description"] == "E2E-LOCKED-DESCRIPTION", (
                    f"{step}: 确认域 description 被覆盖"
                )
                assert lr["probe_status"] == "CONFIRMED", (
                    f"{step}: 确认状态被重置 {lr['probe_status']}"
                )
                assert _member_tables_of(lr) == locked_members, (
                    f"{step}: 确认域成员未冻结: {sorted(_member_tables_of(lr))}"
                )
                if skipped_id is not None:
                    assert skipped_id not in by_id, f"{step}: skip 行未被删除"

            def check_new_rows(new_rows: list[dict], step: str) -> set[str]:
                """本轮新行：可复用锁定域名，但不重复锁定成员；返回覆盖的表。"""
                covered: set[str] = set()
                for r in new_rows:
                    assert (
                        r["probe_status"] == "PENDING" and r["source_type"] == "ALGO"
                    ), (
                        f"{step}: 新行状态异常 {r['domain_name']} "
                        f"{r['probe_status']}/{r['source_type']}"
                    )
                    members = _member_tables_of(r)
                    assert not (members & locked_members), (
                        f"{step}: 新行 {r['domain_name']} 使用了锁定表 "
                        f"{sorted(members & locked_members)}"
                    )
                    assert not (members & covered), (
                        f"{step}: 表被多个待审 Domain 重复认领 "
                        f"{sorted(members & covered)}"
                    )
                    covered |= members
                return covered

            # ==============================================================
            # ③a 完善探查（refine）：PENDING（含 HUMAN）完善；有成员不动 —— 场景 1
            # ==============================================================
            log.info("═══ STEP 3a BIZ_DOMAIN 完善探查（refine：含 HUMAN 空域）═══")
            rows_a = await probe("BIZ_DOMAIN", mode="refine")
            by_id_a = {r["id"]: r for r in rows_a}
            check_locked_frozen(by_id_a, "refine")
            assert not (draft_ids_before & set(by_id_a)), (
                "refine: 旧 ALGO 草稿行未被替换"
            )
            assert human_id not in by_id_a, (
                "refine: 旧 HUMAN 行未被替换（应新增后删除）"
            )
            new_a = [r for r in rows_a if r["id"] not in locked_ids]
            assert 1 <= len(new_a) <= pending_draft_count, (
                "refine 允许多个草稿合并到同一 Domain，但不得无故丢掉全部候选: "
                f"{pending_draft_count} 个草稿 → {len(new_a)} 行"
            )
            # 非空草稿成员不得丢失；多个草稿命中同一 Domain 时成员会取并集。
            members_before = {
                table for member_set in draft_member_sets for table in member_set
            }
            members_after = {table for row in new_a for table in _member_tables_of(row)}
            assert members_before <= members_after, (
                f"refine 丢失原有成员 {sorted(members_before - members_after)}"
            )
            check_new_rows(new_a, "refine")
            for r in new_a:
                assert r["description"], f"refine 后仍无描述: {r['domain_name']}"
            log.info(
                "  ✓ refine: %d 个草稿（含 HUMAN）收口为 %d 行；CONFIRMED 冻结；产出域: %s",
                pending_draft_count,
                len(new_a),
                sorted(r["domain_name"] for r in new_a),
            )

            # ==============================================================
            # ③b 重新探查（create）：删两个草稿后重分域 —— 场景 2 + 3
            # ==============================================================
            log.info(
                "═══ STEP 3b BIZ_DOMAIN 重新探查（create：删两个草稿，锁定域冻结）═══"
            )
            new_a = sorted(new_a, key=lambda r: r["id"])
            deleted = new_a[:2]
            deleted_tables: set[str] = set()
            for r in deleted:
                deleted_tables |= _member_tables_of(r)
                await row_service.delete_row(conn, sdef, _DS_ID, r["id"], admin)
            await conn.commit()
            log.info(
                "  已删除草稿 %s（成员 %d 张表）",
                [r["domain_name"] for r in deleted],
                len(deleted_tables),
            )
            rows_b = await probe("BIZ_DOMAIN", mode="rerun")
            by_id_b = {r["id"]: r for r in rows_b}
            check_locked_frozen(by_id_b, "create")
            assert not ({r["id"] for r in new_a} & set(by_id_b)), (
                "create: 上一轮草稿未被替换"
            )
            new_b = [r for r in rows_b if r["id"] not in locked_ids]
            covered_b = check_new_rows(new_b, "create")
            unlocked_tables = source_tables - locked_members
            coverage_b = len(covered_b & unlocked_tables) / max(1, len(unlocked_tables))
            assert coverage_b >= 0.8, f"create: 未锁定表覆盖率 {coverage_b:.0%} < 80%"
            recovered = len(deleted_tables & covered_b) / max(1, len(deleted_tables))
            assert recovered >= 0.5, (
                f"create: 被删域的成员表仅 {recovered:.0%} 重新归域（数量不要求一致，但表要回来）"
            )
            log.info(
                "  ✓ create: 新域 %d 个（首探 %d），未锁定表覆盖 %.0f%%，被删域成员表回收 %.0f%%",
                len(new_b),
                len(rows),
                coverage_b * 100,
                recovered * 100,
            )

            # ==============================================================
            # ③c 删除全部草稿 + HUMAN default → 重新探查 —— 场景 4
            #    未确认 HUMAN 可被替换；仅 CONFIRMED 冻结
            # ==============================================================
            log.info("═══ STEP 3c 删全部草稿 + HUMAN default → 重新探查 ═══")
            for r in new_b:
                await row_service.delete_row(conn, sdef, _DS_ID, r["id"], admin)
            await conn.commit()
            default_id = await add_human("default", "全局的 domain 只有默认的一个")
            rows_c = await probe("BIZ_DOMAIN", mode="rerun")
            by_id_c = {r["id"]: r for r in rows_c}
            check_locked_frozen(by_id_c, "create(default)")
            assert default_id not in by_id_c, "未确认 HUMAN default 应被替换式写回删除"
            new_c = [r for r in rows_c if r["id"] not in locked_ids]
            covered_c = check_new_rows(new_c, "create(default)")
            coverage_c = len(covered_c & unlocked_tables) / max(1, len(unlocked_tables))
            assert coverage_c >= 0.8, (
                f"create(default): 未锁定表覆盖率 {coverage_c:.0%} < 80%"
            )
            log.info(
                "  ✓ HUMAN default 已替换；未锁定表重新分成 %d 个域，覆盖 %.0f%%",
                len(new_c),
                coverage_c * 100,
            )

            # ==============================================================
            # ④ 确认全部 → complete → promote 正式表
            # ==============================================================
            log.info("═══ STEP 4 BIZ_DOMAIN confirm 全部 → complete → promote ═══")
            await confirm_all_and_complete("BIZ_DOMAIN")
            n_formal = await scalar("SELECT COUNT(*) FROM domain WHERE is_deleted=0")
            assert n_formal >= 1, "正式 domain 为空"
            hit = await scalar(
                "SELECT COUNT(*) FROM domain WHERE is_deleted=0 "
                f"AND domain_name='{locked_name}'"
            )
            assert hit == 1, f"确认域 {locked_name} 未进正式表"
            log.info(
                "  ✓ 正式 domain %d 行（复用同名候选不重复新增），确认域已 promote",
                n_formal,
            )

            # ==============================================================
            # ⑤ DATASET 直通
            # ==============================================================
            log.info('═══ STEP 5 DATASET 直通（env 白名单 ["%%"]）+ 日志式 ═══')
            ds_rows = await probe("DATASET")
            passthrough = [r for r in ds_rows if r["dataset_name"] in source_tables]
            derived = [r for r in ds_rows if r["dataset_name"] not in source_tables]
            assert len(passthrough) >= max(1, len(source_tables) // 2), (
                f"直通 dataset 仅 {len(passthrough)}/{len(source_tables)} —— "
                "domain 归属大面积缺失或直通白名单未生效"
            )
            bad = [
                r["dataset_name"]
                for r in derived
                if "select" not in str(r.get("source") or "").lower()
            ]
            assert not bad, f"非直通 dataset 的 source 不是 SQL: {bad}"
            with_domain = sum(1 for r in ds_rows if r.get("domain_name"))
            log.info(
                "  直通 %d/%d 张表 + 日志式 %d 个（来自 gold SQL）；%d 带 domain 归属",
                len(passthrough),
                len(source_tables),
                len(derived),
                with_domain,
            )
            for r in derived:
                log.info(
                    "    日志式 dataset: %s ← %.80s…",
                    r["dataset_name"],
                    str(r["source"]).replace("\n", " "),
                )
            await confirm_all_and_complete("DATASET")
            assert (
                await scalar("SELECT COUNT(*) FROM dataset WHERE is_deleted=0") == 0
            ), "DATASET complete 只完成审核，不应提前写入正式 Dataset"
            log.info(
                "═══ STEP 5b DATASET_FIELDS 探查 → confirm → Dataset + dataset_field promote ═══"
            )
            field_rows = await probe("DATASET_FIELDS")
            assert field_rows, "dataset_field 探查零产出"
            assert {r["dataset_name"] for r in field_rows} <= {
                r["dataset_name"] for r in ds_rows
            }
            await confirm_all_and_complete("DATASET_FIELDS")
            assert (
                await scalar(
                    "SELECT COUNT(*) FROM dataset_column_meta WHERE is_deleted=0"
                )
                > 0
            )
            n_ds = await scalar("SELECT COUNT(*) FROM dataset WHERE is_deleted=0")
            assert n_ds >= 1, "正式 dataset 为空"
            log.info("  ✓ 正式 dataset %d 行", n_ds)

            # ==============================================================
            # ⑥~⑨ 口径与管理四阶段
            # ==============================================================
            log.info("═══ STEP 6 DIMENSION_CALIBER ═══")
            dim_cal = await probe("DIMENSION_CALIBER")
            assert dim_cal, "维度口径探查零产出"
            log.info(
                "  维度口径 %d 行，示例: %s",
                len(dim_cal),
                [f"{r['dataset_name']}.{r['dimension_name']}" for r in dim_cal[:5]],
            )
            await confirm_all_and_complete("DIMENSION_CALIBER")

            log.info("═══ STEP 7 DIMENSION_MANAGE（DICT 维度侧）═══")
            dims = await probe("DIMENSION_MANAGE")
            assert dims, "维度实体探查零产出"
            log.info(
                "  维度实体 %d 个: %s",
                len(dims),
                [r["dimension_name"] for r in dims][:10],
            )
            await confirm_all_and_complete("DIMENSION_MANAGE")
            n_dim = await scalar(
                "SELECT COUNT(*) FROM dimension_dict WHERE is_deleted=0"
            )
            n_bind = await scalar(
                "SELECT COUNT(*) FROM dataset_dimension WHERE is_deleted=0"
            )
            assert n_dim >= 1 and n_bind >= 1, (
                f"维度 promote 异常: dimension={n_dim} dataset_dimension={n_bind}"
            )
            log.info("  ✓ 正式 dimension_dict %d / 绑定 %d", n_dim, n_bind)

            log.info("═══ STEP 8 METRIC_CALIBER ═══")
            metric_cal = await probe("METRIC_CALIBER")
            if metric_cal:
                log.info(
                    "  指标口径 %d 行，示例: %s",
                    len(metric_cal),
                    [f"{r['dataset_name']}.{r['metric_name']}" for r in metric_cal[:5]],
                )
            else:
                log.warning(
                    "  指标口径零产出（LLM 未在 chinook 判出 metric）—— 放行但记录"
                )
            await confirm_all_and_complete("METRIC_CALIBER")

            log.info("═══ STEP 9 METRIC_MANAGE（DICT 指标侧）═══")
            metrics = await probe("METRIC_MANAGE")
            log.info(
                "  指标实体 %d 个: %s",
                len(metrics),
                [r["metric_name"] for r in metrics][:10],
            )
            await confirm_all_and_complete("METRIC_MANAGE")

            # ==============================================================
            # ⑩ 总态 + 汇总
            # ==============================================================
            log.info("═══ STEP 10 总态与正式表核数 ═══")
            status = await stage_service.get_status(conn, _DS_ID, admin)
            assert status.overall_status == OVERALL_COMPLETED, (
                f"总状态 {status.overall_status} != {OVERALL_COMPLETED}"
            )
            n_metric = await scalar(
                "SELECT COUNT(*) FROM metric_dict WHERE is_deleted=0"
            )
            log.info(
                "E2E 全链路 ✓ 七阶段 COMPLETED | 正式表: domain=%d dataset=%d "
                "dimension_dict=%d dataset_dimension=%d metric_dict=%d | "
                "LLM 真实调用 %d 次（其余命中缓存）| 产物与缓存 → %s",
                n_formal,
                n_ds,
                n_dim,
                n_bind,
                n_metric,
                live["n"],
                _OUT_ROOT / _DS_ID,
            )

            # ==============================================================
            # ⑪ 官方导出器：正式表 → 12-sheet workbook，与模板同形 + 行数对账
            # ==============================================================
            log.info(
                "═══ STEP 11 export_workbook_from_storage → 模板同形校验 + 逐 sheet 对账 ═══"
            )
            from context_manager.graph.semantic_template_io import (
                export_workbook_from_storage,
            )
            from semantic_config.template_contract import IMPORT_ORDER, SHEET_SPECS

            wb = await export_workbook_from_storage(conn, admin, datasource_id=_DS_ID)
            export_path = _OUT_ROOT / _DS_ID / "export.xlsx"
            wb.save(export_path)
            log.info("  导出件已落盘: %s", export_path)

            # -- 同形 ①：导出件 sheet 顺序与表头 == 契约模板面（当前业务列）--
            assert wb.sheetnames == [*IMPORT_ORDER, "_meta"], (
                f"导出件 sheet 顺序不符: {wb.sheetnames}"
            )
            exported_rows: dict[str, int] = {}
            for name in IMPORT_ORDER:
                ws = wb[name]
                header = [c.value for c in ws[1]]
                expected = SHEET_SPECS[name].template_header()
                assert header == expected, (
                    f"导出件 {name} 表头 {header} != 契约 {expected}"
                )
                exported_rows[name] = max(0, ws.max_row - 1)

            # 当前契约生成的模板是表头依据，不再对照历史 v2.1 实例簿。
            log.info("  ✓ %d 张业务/物理 sheet 与当前契约同形", len(IMPORT_ORDER))

            # -- 对账：导出件行数 == 正式表 COUNT（profiling 不写的 sheet 必须为 0 行）--
            # 导出器按数据源取闭包：L1 domain 只含「有本源 dataset 的域」——
            # E2E 人工新增的域没有任何 dataset，按规则不在闭包内（不是丢数据）
            n_domain_scope = await scalar(
                f"SELECT COUNT(DISTINCT domain_id) FROM dataset "
                f"WHERE is_deleted=0 AND datasource_id='{_DS_ID}'"
            )
            log.info(
                "  闭包：正式 domain %d 行，其中有本源 dataset 的 %d 行进导出件（%d 个空域按闭包规则排除）",
                n_formal,
                n_domain_scope,
                n_formal - n_domain_scope,
            )
            expected_rows = {
                "table": await scalar(
                    "SELECT COUNT(*) FROM physical_table WHERE is_deleted=0"
                ),
                "physical_join": await scalar(
                    "SELECT COUNT(*) FROM physical_join WHERE is_deleted=0"
                ),
                "table_column": await scalar(
                    "SELECT COUNT(*) FROM table_column WHERE is_deleted=0"
                ),
                "verified_query": await scalar(
                    "SELECT COUNT(*) FROM verified_query WHERE is_deleted=0"
                ),
                "biz_domain": n_domain_scope,
                "dataset": n_ds,
                "dataset_column": await scalar(
                    "SELECT COUNT(*) FROM dataset_column_meta WHERE is_deleted=0"
                ),
                "dimension": n_dim,
                "dataset_dimension": n_bind,
                "metric": n_metric,
                "metric_formula": await scalar(
                    "SELECT COUNT(*) FROM metric_formula_lib WHERE is_deleted=0"
                ),
            }
            mismatch = {
                k: (exported_rows[k], v)
                for k, v in expected_rows.items()
                if exported_rows[k] != v
            }
            assert not mismatch, (
                f"导出件行数与正式表不符 {{sheet: (导出, 正式表)}}: {mismatch}"
            )
            for name in ("table_column", "verified_query"):
                assert exported_rows[name] == 0, (
                    f"{name} 应为 0 行（profiling 链不写该表）"
                )
            log.info("  ✓ 逐 sheet 行数 == 正式表 COUNT: %s", exported_rows)

            # CALIBER 已负责 canonical name，DICT 不再改名；因此所有已确认
            # 口径都必须能解析到实体并完成 promote，不能再静默断链。
            staged_dim = await scalar(
                "SELECT COUNT(*) FROM (SELECT DISTINCT domain_name, dataset_name, dimension_name "
                "FROM staging_dataset_dimension WHERE is_deleted=0 AND probe_status='CONFIRMED')"
            )
            staged_met = await scalar(
                "SELECT COUNT(*) FROM (SELECT DISTINCT domain_name, dataset_name, metric_name, time_window "
                "FROM staging_metric_caliber WHERE is_deleted=0 AND probe_status='CONFIRMED')"
            )
            assert staged_dim == exported_rows["dataset_dimension"], (
                f"维度口径发生 staging → promote 断链：{staged_dim} → "
                f"{exported_rows['dataset_dimension']}"
            )
            assert staged_met == exported_rows["metric_formula"], (
                f"指标口径发生 staging → promote 断链：{staged_met} → "
                f"{exported_rows['metric_formula']}"
            )
            await conn.close()
        finally:
            settings.db_url = originals
            if os.environ.get("E2E_KEEP_DB") == "1":
                # 保留临时库供落库内容人工核查（逐列对照 tables.py 定义）
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
