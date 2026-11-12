"""后端全链路 E2E（BigQuery JSON 版）：真实 AlgoProvider + spider schema JSON + 七阶段 + prior 闭环。

与 ``test_probe_e2e_algo_sqlite.py`` 同一条主链路（十步同构），差别只在数据源：

- 表快照由 :class:`~profiling.algo.inputs.table_input_bigquery.BigQueryJsonTableInput`
  直接从 spider2 的 schema JSON 目录构造 —— **零 SQL、零凭证、零 BigQuery
  查询费**（真连凭证与真连路径本测试不用）；
- 库由 ``_DB`` 切换（austin / ga360 / …）。分片归并边界按**数据驱动**断言：
  6~8 位日期分片（如 ga360 的 ``ga_sessions_YYYYMMDD``）必须归并成通配族，
  其余名字（如 austin 的 4 位年份 ``incidents_2016``）必须原样保留 ——
  同一套断言对两类库都成立。多子域的库（austin）能完整走到 confirm/skip
  场景；单逻辑表的库（ga360）skip 场景自动放宽（测试内已守卫）；
- 枚举证据来自 JSON 的 ``sample_rows``（构造快照时自带），不开
  ``input_table_enums_enabled``（那是 SQL DISTINCT 探查的开关，BQ 采样
  本就禁用 SELECT DISTINCT）。

主链路十步（与 sqlite 版逐条同构）：

  ① BIZ_DOMAIN 首次探查（prior 为空，纯表名+列注释归域）
  ② 行操作：confirm 一个域（并编辑 description）、skip 一个域、
     人工新增一个 HUMAN 域
  ③ BIZ_DOMAIN 重新探查 —— prior 闭环验收
  ④ confirm 全部 → complete → promote → 正式表 domain
  ⑤ DATASET 直通（env 白名单 ``["%"]``，source=table_fullname 单指针）
     → confirm/complete → DATASET_FIELDS 探查与确认 → Dataset + dataset_field promote
  ⑥~⑨ DIMENSION_CALIBER / DIMENSION_MANAGE / METRIC_CALIBER / METRIC_MANAGE
  ⑩ 总态 OVERALL_COMPLETED + 正式表核数 + LLM 真实调用数汇总
  ⑪ 官方导出器 export_workbook_from_storage → 12-sheet workbook：与模板同形校验 + 逐 sheet 与正式表对账

前提与费用：需要 ``data/Spider2`` 与 .env 的 ``OPENAI_API_KEY``，缺任一则
skip；LLM 响应缓存落 ``data/profile/e2e/e2e-bq-<库名>/llm/``，参数不变的
重跑近乎免费，首跑约 15~25 次真实调用（库越小越少）。

运行（日志全量输出到终端）::

    cd packages/qwenpaw-data-context
    uv run --no-sync --with pytest python -m pytest \
        tests/profiling/integration/e2e/test_probe_e2e_algo_bigquery.py -s --log-cli-level=INFO
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

import pytest

log = logging.getLogger("test_probe_e2e_bq")

#: 与 ``merge_shard_families`` 同一条规则（6~8 位日期后缀）——测试侧独立
#: 声明一份，才能作为归并行为的**交叉验证**而不是同义反复。
_SHARD_SUFFIX = re.compile(r"_\d{6,8}$")

_PKG = Path(__file__).resolve().parents[4]  # packages/qwenpaw-data-context
_REPO = Path(__file__).resolve().parents[6]  # 仓库根
_DB = "ga360"
_SCHEMA_DIR = (
    _REPO
    / "data"
    / "Spider2"
    / "spider2-lite"
    / "resource"
    / "databases"
    / "bigquery"
    / _DB
)
_HOME_ROOT = Path(tempfile.gettempdir()) / "qwenpaw-data-profiling-e2e-home"
_OUT_ROOT = _HOME_ROOT / "t1" / "w1" / ".cm" / "profile"
_DS_ID = f"e2e-bq-{_DB}"  # 固定 → 跨运行共享 LLM 缓存
_SPIDER_LITE = _REPO / "data" / "Spider2" / "spider2-lite"
_GOLD_INDEX = _SPIDER_LITE / "spider2-lite.jsonl"  # 每行 {"instance_id", "db", ...}
_GOLD_DIR = _SPIDER_LITE / "evaluation_suite" / "gold" / "sql"

#: 与 sqlite E2E 相同：Provider 注入身份、模式和目录，算法参数走 env；
#: spider 前提只能经部署环境变量注入。
_ENV_OVERRIDES = {
    "EXECUTE_SQL_PATH": "",  # JSON 来源零 SQL
    "QWENPAW_DATA_HOME": str(_HOME_ROOT),  # 在线 Provider 的稳定缓存目录
    "STORE_OUT_DIR": str(_OUT_ROOT),  # 离线回读同一目录
    "LLM_OUTPUT_LANGUAGE": "en",  # spider 全英文
    "DATASET_DERIVED_FROM_TABLES_INCLUDE_PATTERNS": '["%"]',  # 全表直通
    "DATASET_PARSE_FROM_LOGS_ENABLED": "true",  # gold SQL 也作日志式 dataset 来源
    # -- 查询日志 = 评测集 gold SQL（文件来源，零 SQL 执行）--
    "INPUT_QUERY_LOG_DATASOURCE_TYPE": "sql_files",
    "INPUT_QUERY_LOG_PATH": str(_GOLD_INDEX),
    "INPUT_QUERY_LOG_DIR": str(_GOLD_DIR),
    "INPUT_QUERY_LOG_DBNAME": _DB,
    "PARSER_DIALECT": "bigquery",  # 分片形态按 bigquery 折成族名
    "PARSER_TEMPLATE_MIN_COUNT": "1",  # 1~12 条 gold，不可能重复 10 次
    "PARSER_TEMPLATE_MIN_COLUMNS": "3",  # gold 是答题查询，投影窄
}


# ===========================================================================
# 工具
# ===========================================================================


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


async def _source_tables(algo_config) -> set[str]:
    """库的逻辑表名 —— 用与 provider 完全相同的 JsonInput 代码路径算
    期望集（含跨 dataset 重名限定与分片归并），断言口径零漂移。"""
    from profiling.algo.inputs.table_input_bigquery import BigQueryJsonTableInput

    cfg = algo_config.AlgoConfig.build(
        datasource_type="bigquery_json",
        execute_sql_path="",
        datasource_connection={"schema_dir": str(_SCHEMA_DIR)},
    )
    meta = await BigQueryJsonTableInput(cfg).fetch_table_meta()
    return {str(n) for n in meta["table_name"]}


def _raw_table_names() -> set[str]:
    """schema JSON 里的**原始**表名（归并前）。独立于 JsonInput 读取，
    供归并边界的交叉验证。"""
    names: set[str] = set()
    for path in _SCHEMA_DIR.rglob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        names.add(str(record.get("table_name") or path.stem))
    return names


def _contains(logical: set[str], name: str) -> bool:
    """name 在逻辑表集中（跨 dataset 重名会被限定成 ``<dataset>.<name>``）。"""
    return name in logical or any(t.endswith(f".{name}") for t in logical)


def _assert_shard_merge_boundary(source_tables: set[str]) -> None:
    """数据驱动的归并边界：6~8 位日期分片必须归并成通配族且裸名消失；
    其余名字（含 4 位年份等短后缀）必须原样保留。对 austin / ga360 都成立。"""
    raw = _raw_table_names()
    shard_raw = {n for n in raw if _SHARD_SUFFIX.search(n)}
    for name in sorted(shard_raw):
        family = _SHARD_SUFFIX.sub("_*", name)
        assert _contains(source_tables, family), (
            f"日期分片 {name} 未归并出通配族 {family}"
        )
        assert not _contains(source_tables, name), f"日期分片 {name} 归并后仍以裸名存在"
    for name in sorted(raw - shard_raw):
        assert _contains(source_tables, name), f"非分片表 {name} 被过度归并（丢失原名）"
    log.info(
        "  归并边界 ✓ 原始 %d 张 → 逻辑 %d 张（分片 %d 张归并为 %d 个族）",
        len(raw),
        len(source_tables),
        len(shard_raw),
        len({_SHARD_SUFFIX.sub("_*", n) for n in shard_raw}),
    )


# ===========================================================================
# 主链路
# ===========================================================================


def test_e2e_full_backend_chain_bigquery_json():
    if not _SCHEMA_DIR.is_dir():
        pytest.skip(f"Spider2 BigQuery schema 目录缺失: {_SCHEMA_DIR}")

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
        conn = None
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
                f"E2E bigquery {_DB}",
                "BIGQUERY_JSON",
                json.dumps({"schema_dir": str(_SCHEMA_DIR)}),
                tenant_id=admin.tenant_id,
                workspace_id=admin.active_workspace_id,
                created_by=admin.user_id,
            )
            await conn.commit()
            source_tables = await _source_tables(algo_config)
            log.info(
                "  数据源已登记：%d 张逻辑表 %s",
                len(source_tables),
                sorted(source_tables),
            )
            _assert_shard_merge_boundary(source_tables)

            async def scalar(sql: str) -> int:
                res = await conn.execute(text(sql))
                val = res.scalar()
                return val if val is not None else 0

            async def probe(stage: str, *, mode: str = "create") -> list[dict]:
                """提交探查 / 重新探查 → 等结束 → 校验任务 SUCCESS → 返回 staging 行。"""
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
                    f"{stage} {label}失败: status={task.status} message={task.message} error={task.error_msg}"
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
                    datasource_type="BIGQUERY_JSON",
                    datasource_connection={"schema_dir": str(_SCHEMA_DIR)},
                )
                logs = await AlgoContexts.build(cfg, offline=True).query_log.load()
                return logs.of(kind)

            log.info(
                "═══ STEP 1 BIZ_DOMAIN 首次探查（prior 应为空，gold SQL 作日志）═══"
            )
            rows = await probe("BIZ_DOMAIN")
            assert rows, "首次探查没有产出任何业务域"
            # BigQuery gold 多为 CTE / UNNEST 的单表分析，join 边可能为空；
            # 列级用法证据必有，且表名必须已按 bigquery 折成族名落在表面上
            usage = await log_evidence("dimension_usage")
            joins = await log_evidence("join")
            assert len(usage) > 0, f"{_DB} 的 gold SQL 没有抽出任何列级用法证据"
            referenced = {str(n) for n in usage["table_name"] if str(n) != "_"}
            referenced |= {
                str(n) for c in ("left_table", "right_table") for n in joins[c]
            }
            unknown = referenced - source_tables
            assert not unknown, (
                f"日志证据表名未与表面对齐（分片归一失效？）: {sorted(unknown)}"
            )
            log.info(
                "  日志证据 ✓ dimension_usage %d 行 / join %d 条边，引用表 %s 均在表面上",
                len(usage),
                len(joins),
                sorted(referenced),
            )
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
            # ② 行操作：confirm+编辑 / skip / 人工新增
            # ==============================================================
            log.info("═══ STEP 2 行操作（confirm+edit / skip / HUMAN 新增）═══")
            sdef = def_by_stage("BIZ_DOMAIN")
            locked_row = rows[0]
            locked_name = locked_row["domain_name"]
            locked_members = _member_tables_of(locked_row)
            await row_service.update_row(
                conn,
                sdef,
                _DS_ID,
                locked_row["id"],
                {"description": "E2E-BQ-LOCKED-DESCRIPTION"},
                admin,
            )
            await row_service.batch_confirm(
                conn, sdef, _DS_ID, [locked_row["id"]], admin
            )
            log.info(
                "  已确认并修改描述: %s（成员 %s）", locked_name, sorted(locked_members)
            )

            skipped_id = skipped_name = None
            if len(rows) >= 2:
                skipped_id, skipped_name = rows[1]["id"], rows[1]["domain_name"]
                await row_service.batch_skip(conn, sdef, _DS_ID, [rows[1]["id"]], admin)
                log.info("  已否决: %s", skipped_name)
            else:
                log.warning("  首探只有 1 个域，跳过 skip 场景（断言将相应放宽）")

            human_name = "e2e_bq_manual_domain"
            await row_service.add_row(
                conn,
                sdef,
                _DS_ID,
                {
                    "domain_name": human_name,
                    "description": "human-added row, must survive re-probe",
                },
                admin,
            )
            await conn.commit()
            log.info("  已人工新增（source_type=HUMAN）: %s", human_name)

            # ==============================================================
            # ③ BIZ_DOMAIN 重新探查 —— prior 闭环验收
            # ==============================================================
            log.info("═══ STEP 3 BIZ_DOMAIN 重新探查（prior 必须生效）═══")
            rows2 = await probe("BIZ_DOMAIN", mode="rerun")
            names2 = {r["domain_name"] for r in rows2}
            log.info("  重探产出域: %s", sorted(names2))

            assert locked_name in names2, (
                f"用户确认过的域 {locked_name} 在重探后丢失 —— prior 未生效"
            )
            locked_row2 = next(r for r in rows2 if r["id"] == locked_row["id"])
            assert locked_row2["probe_status"] == "CONFIRMED"
            members2 = _member_tables_of(locked_row2)
            assert locked_members == members2, (
                f"确认域 {locked_name} 的成员被移出: 原 {sorted(locked_members)} "
                f"→ 现 {sorted(members2)}"
            )
            assert locked_row2["description"] == "E2E-BQ-LOCKED-DESCRIPTION", (
                f"确认域描述被 LLM 覆盖: {locked_row2['description']!r} —— "
                "元信息未按既有为准"
            )
            log.info("  ✓ 确认域保住：名称/成员/编辑过的 description 均未被覆盖")

            # 复用允许同名增量候选；锁定的是已确认成员，不是 Domain 名称。
            owner_by_table = {}
            for row in rows2:
                if row["probe_status"] == "SKIPPED":
                    continue
                members = _member_tables_of(row)
                if row["id"] != locked_row["id"]:
                    assert not members & locked_members, "重探重复输出了锁定成员"
                for table in members:
                    assert table not in owner_by_table, (
                        f"{table} 出现在多个有效 Domain 行"
                    )
                    owner_by_table[table] = row["domain_name"]
            if skipped_id is not None:
                assert skipped_id not in {r["id"] for r in rows2}, (
                    "旧 SKIPPED 行应被替换"
                )
                # 被拒绝的名称/成员可以重新提议，不是永久黑名单。

            # 未确认 HUMAN 不再冻结：重探后可能被替换，不再要求同名保留
            if human_name in names2:
                human_row2 = next(r for r in rows2 if r["domain_name"] == human_name)
                log.info(
                    "  · HUMAN 域名仍在产出中: %s（%s/%s）",
                    human_name,
                    human_row2["source_type"],
                    human_row2["probe_status"],
                )
            else:
                log.info("  ✓ 未确认 HUMAN 域已被替换式写回（符合仅 CONFIRMED 冻结）")

            # ==============================================================
            # ④ 确认全部 → complete → promote 正式表
            # ==============================================================
            log.info("═══ STEP 4 BIZ_DOMAIN confirm 全部 → complete → promote ═══")
            await confirm_all_and_complete("BIZ_DOMAIN")
            n_formal = await scalar("SELECT COUNT(*) FROM domain WHERE is_deleted=0")
            assert n_formal == len(
                {
                    r["domain_name"]
                    for r in rows2
                    if r["probe_status"] in {"CONFIRMED", "PENDING"}
                }
            ), "正式 Domain 数量应等于已采纳的去重名称数"
            hit = await scalar(
                "SELECT COUNT(*) FROM domain WHERE is_deleted=0 "
                f"AND domain_name='{locked_name}'"
            )
            assert hit == 1, f"确认域 {locked_name} 未进正式表"
            log.info("  ✓ 正式 domain %d 行，确认域已 promote", n_formal)

            # ==============================================================
            # ⑤ DATASET 直通（source = table_fullname 单指针）
            # ==============================================================
            log.info('═══ STEP 5 DATASET 直通（env 白名单 ["%%"]）+ 日志式 ═══')
            ds_rows = await probe("DATASET")
            passthrough = [r for r in ds_rows if r["dataset_name"] in source_tables]
            derived = [r for r in ds_rows if r["dataset_name"] not in source_tables]
            assert len(passthrough) >= max(1, len(source_tables) // 2), (
                f"直通 dataset 仅 {len(passthrough)}/{len(source_tables)} —— "
                "domain 归属大面积缺失或直通白名单未生效"
            )
            no_source = [r["dataset_name"] for r in ds_rows if not r.get("source")]
            assert not no_source, f"dataset 缺 source 单指针: {no_source}"
            bad = [
                r["dataset_name"]
                for r in derived
                if "select" not in str(r.get("source") or "").lower()
            ]
            assert not bad, f"非直通 dataset 的 source 不是 SQL: {bad}"
            with_domain = sum(1 for r in ds_rows if r.get("domain_name"))
            sample = passthrough[0]
            log.info(
                "  直通 %d/%d 张表 + 日志式 %d 个（来自 gold SQL）；%d 带 domain 归属；直通 source 样例: %s → %s",
                len(passthrough),
                len(source_tables),
                len(derived),
                with_domain,
                sample["dataset_name"],
                sample["source"],
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
                    "  指标口径零产出（LLM 未在 %s 判出 metric）—— 放行但记录", _DB
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
                "E2E(BQ-JSON) 全链路 ✓ 七阶段 COMPLETED | 正式表: domain=%d "
                "dataset=%d dimension_dict=%d dataset_dimension=%d metric_dict=%d | "
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
        finally:
            from semantic_config.engine import reset_engine

            if conn is not None:
                await conn.close()
            await reset_engine()
            settings.db_url = originals
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
