"""完成约束校验 + promote（staging → 正式实体表），对齐 SPEC §6.4 与 MG v2.1 契约。

- 候选态阶段（业务域/数据集/维度口径/指标口径）：所有行须 ∈ {CONFIRMED, SKIPPED}，否则 400。
- 口径阶段（DIMENSION_CALIBER/METRIC_CALIBER）完成时**不 promote**，仅置 COMPLETED；其 staging
  保留，供紧随的管理阶段一并落库（口径关联表需要管理阶段 promote 出的实体 id）。
- 管理阶段整体确认 PENDING/CONFIRMED 管理项；排除 SKIPPED，校验后晋升实体及已确认口径。
- 正式写入统一调用 semantic_config service，由其校验、派生关联字段并维护审计属性。
  profiling 负责候选审核、身份解析和复用冲突检查。已有定义保留；
  正式 Dataset 必须有业务域；缺失归属时报错，已有归属不能迁移：

  ==================  =========================================
  domain              (tenant, workspace, domain_name)
  dataset             (datasource_id, dataset_name)
  dataset_field       (dataset_id, field_name)
  dimension_dict      (domain_id, dimension_name)
  dataset_dimension   (dataset_id, dimension_id)
  metric_dict         (domain_id, metric_name)
  metric_caliber      (metric_id, dataset_id, time_window)
  ==================  =========================================

- 域层实体按 tenant/workspace 读取；口径解析父数据集时按 ``dataset.datasource_id``
  隔离。Dataset 同源同名只能属于一个 Domain，归属冲突在写入前拒绝。
  Domain 的 datasource_scope 由 semantic_config 在数据集写入时维护，profiling 不写此字段。
- 复用冲突在首次正式写入前拒绝；正式定义不隐式覆盖，同批新建项按待写内容去重。
  service 的业务校验失败由请求事务整体回滚，阶段不标记为完成。
- NOT NULL 前置校验：``dataset.source`` / ``dataset_dimension.expr`` /
  ``metric_caliber.(expr, time_window)`` 缺失时 400 指名道姓 —— pydantic 会静默
  丢弃未知字段，靠数据库约束报错时已经丢了上下文。
- 字段及两类口径的 ``datasource_id / domain_id`` 由 SC service 从父数据集派生。
- complete 在请求单连接内完成（get_db 请求级 commit/rollback 即事务边界）。
"""

from __future__ import annotations

import json

from qwenpaw_data_auth import Principal
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from profiling.models.probe import ROW_CONFIRMED, STATUS_COMPLETED
from semantic_config.tables import (
    dataset,
    dataset_dimension,
    dataset_field,
    dimension_dict,
    domain,
    metric_caliber,
    metric_dict,
)
from profiling.repositories import probe_stage_repo as stage_repo
from profiling.repositories import staging_repo
from profiling.stages import STAGE_DEFS
from semantic_config.errors import BadRequestError
from semantic_config.models.biz_domain import BizDomainCreate
from semantic_config.models.dataset_dimension import DatasetDimensionCreate
from semantic_config.models.dataset_field import DatasetFieldCreate
from semantic_config.models.dataset_meta import DatasetMetaCreate
from semantic_config.models.dimension import DimensionCreate
from semantic_config.models.metric_caliber import CaliberCreate
from semantic_config.models.metric_lib import MetricCreate
from semantic_config.services import (
    biz_domain_service,
    dataset_dimension_service,
    dataset_field_service,
    dataset_meta_service,
    dimension_service,
    metric_caliber_service,
    metric_lib_service,
)

_COMPLETE_MSG = {
    "BIZ_DOMAIN": "请确保所有业务域已确定或已跳过",
    "DATASET": "请确保所有数据集已确定或已跳过",
    "DATASET_FIELDS": "请确保所有数据集字段已确定或已跳过",
    "DIMENSION_CALIBER": "请确保所有维度口径已确定或已跳过",
    "METRIC_CALIBER": "请确保所有指标口径已确定或已跳过",
}


async def _rows(db, stmt):
    res = await db.execute(stmt)
    return res.mappings().all()


def _scoped_domain(table, tid, wid):
    """作用域内的活跃正式实体或关系，不按来源数据源限制共享目录。"""
    return (
        table.c.tenant_id == tid,
        table.c.workspace_id == wid,
        table.c.is_deleted == 0,
    )


def _text(value) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _need_name(value, what: str) -> str:
    """名称键列（NOT NULL + 去重自然键）的前置校验：NULL 打穿到库是 500
    丢上下文，空白串落成 '' 是无名实体占住去重键 —— 都在这里 400 拦下。"""
    name = _text(value)
    if name is None:
        raise BadRequestError(f"{what}名称为空，请补齐后再完成")
    return name


def _require_dataset_domain(domain_id, what: str) -> None:
    if domain_id is None:
        raise BadRequestError(
            f"{what} 缺少业务域（domain_id 为空），请先修复正式数据后重试"
        )


def _resolve_domain_id(row, domain_name_to_id: dict[str, int], what: str) -> int:
    """Resolve the formal Domain ID from the canonical name selected by profiling."""
    what = f"{what}（staging id={row['id']}）"
    domain_name = _need_name(row["domain_name"], f"{what}的业务域")
    domain_id = domain_name_to_id.get(domain_name)
    if domain_id is None:
        raise BadRequestError(
            f"{what}引用的业务域 {domain_name!r} 不存在或尚未晋升，"
            "请先完成 BIZ_DOMAIN 阶段"
        )
    return domain_id


def _resolve_caliber_dataset_id(
    row,
    datasource_id: str,
    domain_id: int,
    dataset_key_to_id: dict[tuple, int],
    dataset_identity: dict[int, tuple[str | None, int | None, str]],
) -> int | None:
    """Accept a staged Dataset ID only when it belongs to the current source."""
    dataset_name = _text(row["dataset_name"])
    expected_identity = (datasource_id, domain_id, dataset_name)
    dataset_id = row["dataset_id"]
    identity = dataset_identity.get(dataset_id)
    if (
        identity is not None
        and identity[0] == datasource_id
        and (dataset_name is None or dataset_name == identity[2])
    ):
        _require_dataset_domain(identity[1], f"正式 Dataset {identity[2]!r}")
    if dataset_key_to_id.get((None, dataset_name)) is not None:
        _require_dataset_domain(None, f"正式 Dataset {dataset_name!r}")
    if identity is not None and (
        identity == expected_identity
        or (dataset_name is None and identity[:2] == (datasource_id, domain_id))
    ):
        return dataset_id
    return dataset_key_to_id.get((domain_id, dataset_name))


async def _validate_all_terminal(db, table, datasource_id, tid, wid, stage) -> None:
    rows = await staging_repo.list_all(db, table, datasource_id, tid, wid)
    if any(r["probe_status"] not in (ROW_CONFIRMED, "SKIPPED") for r in rows):
        raise BadRequestError(_COMPLETE_MSG.get(stage, "请确保所有结果已确定或已跳过"))


async def _set_completed(db, datasource_id, stage, principal) -> None:
    row = await stage_repo.find_one(
        db,
        datasource_id,
        stage,
        principal.tenant_id,
        principal.active_workspace_id,
    )
    await stage_repo.update_status(
        db,
        datasource_id,
        stage,
        STATUS_COMPLETED,
        tenant_id=principal.tenant_id,
        workspace_id=principal.active_workspace_id,
        updated_by=principal.user_id,
        expected_status=row["status"] if row is not None else None,
        expected_task_id=row["current_task_id"] if row is not None else None,
    )


# ---- 小型校验工具：只读准备，所有正式写入都在各入口的末尾 ----
async def _formal_index(db, table, columns, tid, wid, *conditions) -> dict:
    """保留完整记录用于比较；正式业务键有歧义时不能任取一个 ID。"""
    result = {}
    for row in await _rows(
        db, select(table).where(*_scoped_domain(table, tid, wid), *conditions)
    ):
        key = tuple(
            _text(row[c]) if isinstance(row[c], str) else row[c] for c in columns
        )
        if key in result:
            raise BadRequestError(
                f"{table.name} 业务身份 {dict(zip(columns, key))!r} 存在多条活跃正式记录："
                f"正式 id={result[key]['id']} 与正式 id={row['id']}；"
                "请先在正式资产管理中消除歧义，再重试完成"
            )
        result[key] = row
    return result


def _payload(row, *names, **defaults) -> dict:
    """仅首尾空白和现有默认值归一，不做 SQL、类型或语义等价推断。"""
    return {
        name: _text(row.get(name)) or defaults.get(name) for name in (*names, *defaults)
    }


def _check_same(what, key, row, payload, previous, previous_payload, *, formal=False):
    differences = [
        f"{name}: {previous_payload.get(name)!r} -> {value!r}"
        for name, value in payload.items()
        if previous_payload.get(name) != value
    ]
    if differences:
        previous_kind = "正式" if formal else "staging"
        raise BadRequestError(
            f"{what}冲突：业务身份 {key!r}；staging id={row['id']} 与"
            f"{previous_kind} id={previous['id']}；差异属性：{'; '.join(differences)}。"
            "请统一候选定义或仅保留一条确认、跳过其他候选；"
            "若需修改正式定义，请在正式资产管理中处理后重试"
        )


def _prepare_new(prepared, key, row, payload, what):
    """同批新建项按实际待写内容去重，保留行身份以解释冲突。"""
    if key in prepared:
        previous, previous_payload = prepared[key]
        _check_same(what, key, row, payload, previous, previous_payload)
    else:
        prepared[key] = (row, payload)


async def _domain_name_to_id(db, tid, wid) -> dict[str, int]:
    records = await _formal_index(db, domain, ("domain_name",), tid, wid)
    return {key[0]: row["id"] for key, row in records.items()}


async def _dataset_records(db, datasource_id, tid, wid):
    return await _formal_index(
        db,
        dataset,
        ("datasource_id", "dataset_name"),
        tid,
        wid,
        dataset.c.datasource_id == datasource_id,
    )


async def _caliber_dataset_maps(db, datasource_id, tid, wid):
    records = await _dataset_records(db, datasource_id, tid, wid)
    return (
        {(r["domain_id"], key[1]): r["id"] for key, r in records.items()},
        {
            r["id"]: (datasource_id, r["domain_id"], key[1])
            for key, r in records.items()
        },
        {r["id"]: r["domain_id"] for r in records.values()},
    )


def _member_names(row) -> set[str]:
    raw = row.get("member_tables")
    if raw is None or raw == "":
        return set()
    try:
        members = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(members, list):
            raise ValueError
        names = set()
        for member in members:
            name = (
                _text(member.get("source_table")) if isinstance(member, dict) else None
            )
            if name is None:
                raise ValueError
            names.add(name)
        return names
    except (ValueError, TypeError):
        raise BadRequestError(
            f"Domain {row['domain_name']!r} staging id={row['id']} 的 member_tables "
            "必须为包含非空 source_table 的数组，请重新生成或修正后再完成"
        ) from None


# ============================ 各阶段 complete ============================
async def complete(
    db: AsyncSession, datasource_id: str, stage: str, principal: Principal
) -> dict:
    handler = {
        "BIZ_DOMAIN": _complete_biz_domain,
        # DATASET 只定稿数据集清单，落库推迟到 DATASET_FIELDS —— 见
        # ``_complete_dataset`` 的 docstring（同 CALIBER → MANAGE 先例）
        "DATASET": _complete_review_only,
        "DATASET_FIELDS": _complete_dataset,
        "DIMENSION_CALIBER": _complete_review_only,
        "DIMENSION_MANAGE": _complete_dimension_manage,
        "METRIC_CALIBER": _complete_review_only,
        "METRIC_MANAGE": _complete_metric_manage,
    }[stage]
    return await handler(db, datasource_id, stage, principal)


async def _complete_biz_domain(db, datasource_id, stage, principal) -> dict:
    tid, wid = principal.tenant_id, principal.active_workspace_id
    table = STAGE_DEFS[stage].table
    await _validate_all_terminal(db, table, datasource_id, tid, wid, stage)
    existing = await _domain_name_to_id(db, tid, wid)
    formal_datasets = await _dataset_records(db, datasource_id, tid, wid)
    rows = await staging_repo.list_all(
        db, table, datasource_id, tid, wid, statuses=(ROW_CONFIRMED,)
    )
    prepared, members_by_domain, member_owners = {}, {}, {}
    # 仅 source 的精确值可锚定物理成员；不使用 Dataset 展示名称或 SQL 血缘。
    source_owners = {}
    for record in formal_datasets.values():
        source_owners.setdefault(_text(record["source"]), []).append(record)
    for row in rows:
        name = _need_name(row["domain_name"], f"Domain staging id={row['id']} ")
        payload = dict(domain_name=name, description=_text(row["description"]))
        if name not in existing:
            _prepare_new(prepared, name, row, payload, "Domain")
        members = _member_names(row)
        members_by_domain.setdefault(name, set()).update(members)
        for member in members:
            key = (datasource_id, member)
            if member in member_owners:
                owner, previous = member_owners[member]
                _check_same(
                    "Domain member_tables",
                    key,
                    row,
                    {"domain_name": name},
                    previous,
                    {"domain_name": owner},
                )
            else:
                member_owners[member] = (name, row)
    for name, members in members_by_domain.items():
        for member in members:
            _, row = member_owners[member]
            for record in source_owners.get(member, ()):
                _require_dataset_domain(
                    record["domain_id"], f"正式 Dataset {record['dataset_name']!r}"
                )
                _check_same(
                    "Domain member_tables（已有 Dataset 归属）",
                    (datasource_id, member),
                    row,
                    {"domain_id": existing.get(name, f"尚未创建的 Domain {name!r}")},
                    record,
                    {"domain_id": record["domain_id"]},
                    formal=True,
                )

    for _, payload in prepared.values():
        await biz_domain_service.create(db, BizDomainCreate(**payload), principal)
    # 成员只参与校验/合并，staging 原行保留；scope 的初始化和派生归 SC 负责。
    await _set_completed(db, datasource_id, stage, principal)
    return {"stage": stage, "status": STATUS_COMPLETED, "promoted": len(prepared)}


async def _complete_dataset(db, datasource_id, stage, principal) -> dict:
    """DATASET_FIELDS 晋升数据集/字段；已有 Dataset 必须有业务域。"""
    tid, wid = principal.tenant_id, principal.active_workspace_id
    await _validate_all_terminal(
        db, STAGE_DEFS[stage].table, datasource_id, tid, wid, stage
    )
    rows = await staging_repo.list_all(
        db, "staging_dataset", datasource_id, tid, wid, statuses=(ROW_CONFIRMED,)
    )
    domain_names = await _domain_name_to_id(db, tid, wid)
    existing = await _dataset_records(db, datasource_id, tid, wid)
    dataset_ids = {key: row["id"] for key, row in existing.items()}
    dataset_domains = {key: row["domain_id"] for key, row in existing.items()}
    id_to_key = {row["id"]: key for key, row in existing.items()}
    prepared_datasets = {}
    for row in rows:
        name = _need_name(row["dataset_name"], f"Dataset staging id={row['id']} ")
        what = f"数据源 {datasource_id!r} 的数据集 {name!r}"
        domain_id = _resolve_domain_id(row, domain_names, what)
        source = _text(row["source"])
        if source is None:
            raise BadRequestError(
                f"{what} staging id={row['id']} 缺少 source（表名或 SQL），请补齐后再完成"
            )
        key = (datasource_id, name)
        payload = dict(
            datasource_id=datasource_id,
            dataset_name=name,
            domain_id=domain_id,
            source=source,
            description=_text(row["description"]),
        )
        previous = existing.get(key)
        if previous is not None:
            _require_dataset_domain(previous["domain_id"], f"正式{what}")
            _check_same(
                f"{what} 业务域归属" if previous["domain_id"] != domain_id else what,
                key,
                row,
                {"domain_id": domain_id, "source": source},
                previous,
                {
                    "domain_id": previous["domain_id"],
                    "source": _text(previous["source"]),
                },
                formal=True,
            )
        else:
            label = (
                f"{what} 业务域归属"
                if (key in dataset_domains and dataset_domains[key] != domain_id)
                else what
            )
            _prepare_new(prepared_datasets, key, row, payload, label)
        dataset_domains[key] = domain_id

    existing_fields = await _formal_index(
        db,
        dataset_field,
        ("dataset_id", "field_name"),
        tid,
        wid,
        dataset_field.c.dataset_id.in_(dataset_ids.values()),
    )
    prepared_fields = {}
    field_rows = await staging_repo.list_all(
        db, "staging_dataset_field", datasource_id, tid, wid, statuses=(ROW_CONFIRMED,)
    )
    for row in field_rows:
        name = _need_name(row["field_name"], f"dataset_field staging id={row['id']} ")
        dataset_name = _text(row["dataset_name"])
        # 名称存在时按本源名称解析；仅无名称时才接受已核验的本源正式 ID。
        parent_key = (
            (datasource_id, dataset_name)
            if dataset_name is not None
            else id_to_key.get(row["dataset_id"])
        )
        if parent_key not in dataset_domains:
            raise BadRequestError(
                f"dataset_field {name!r} staging id={row['id']} 无法解析当前数据源 "
                f"{datasource_id!r} 的父 Dataset（dataset_name={dataset_name!r}, "
                f"dataset_id={row['dataset_id']!r}）；请确认父 Dataset 或修正引用后再完成"
            )
        _require_dataset_domain(
            dataset_domains[parent_key],
            f"字段 {name!r} 的正式 Dataset {parent_key[1]!r}",
        )
        key = (parent_key, name)
        payload = dict(
            field_name=name,
            **_payload(
                row, "description", "data_type", is_primary="N", is_nullable="Y"
            ),
        )
        previous = existing_fields.get((dataset_ids.get(parent_key), name))
        if previous is not None:
            attributes = ("data_type", "is_primary", "is_nullable")
            _check_same(
                "dataset_field",
                key,
                row,
                {a: payload[a] for a in attributes},
                previous,
                _payload(previous, "data_type", is_primary="N", is_nullable="Y"),
                formal=True,
            )
        else:
            _prepare_new(prepared_fields, key, row, payload, "dataset_field")

    for key, (_, payload) in prepared_datasets.items():
        created = await dataset_meta_service.create(
            db, DatasetMetaCreate(**payload), principal
        )
        dataset_ids[key] = created.dataset_id
    for (parent_key, _), (_, payload) in prepared_fields.items():
        await dataset_field_service.create(
            db,
            DatasetFieldCreate(dataset_id=dataset_ids[parent_key], **payload),
            principal,
        )
    await _set_completed(db, datasource_id, stage, principal)
    return {
        "stage": stage,
        "status": STATUS_COMPLETED,
        "promoted": len(prepared_datasets),
        "promoted_dataset_field": len(prepared_fields),
    }


async def _complete_review_only(db, datasource_id, stage, principal) -> dict:
    """DATASET / DIMENSION_CALIBER / METRIC_CALIBER：校验后仅置 COMPLETED，不 promote。

    这三个阶段都只是「本阶段的候选定稿了」，正式落库交给下游那个持有完整
    实体的阶段：数据集 → DATASET_FIELDS，口径 → 对应的 MANAGE。staging 全程保留。
    """
    tid, wid = principal.tenant_id, principal.active_workspace_id
    table = STAGE_DEFS[stage].table
    await _validate_all_terminal(db, table, datasource_id, tid, wid, stage)
    await _set_completed(db, datasource_id, stage, principal)
    return {"stage": stage, "status": STATUS_COMPLETED, "promoted": 0}


async def _complete_dimension_manage(db, datasource_id, stage, principal) -> dict:
    tid, wid = principal.tenant_id, principal.active_workspace_id
    domain_names = await _domain_name_to_id(db, tid, wid)
    existing = await _formal_index(
        db, dimension_dict, ("domain_id", "dimension_name"), tid, wid
    )
    dim_ids = {key: row["id"] for key, row in existing.items()}
    rows = await staging_repo.list_all(
        db,
        "staging_dimension",
        datasource_id,
        tid,
        wid,
        statuses=("PENDING", ROW_CONFIRMED),
    )
    prepared_dims = {}
    for row in rows:
        name = _need_name(row["dimension_name"], f"Dimension staging id={row['id']} ")
        domain_id = _resolve_domain_id(row, domain_names, f"维度 {name!r}")
        key = (domain_id, name)
        payload = dict(
            domain_id=domain_id,
            dimension_name=name,
            **_payload(
                row,
                "description",
                "dimension_type",
                "child_dimensions",
                synonyms="",
                priority="MEDIUM",
            ),
        )
        previous = existing.get(key)
        if previous is not None:
            old_type, new_type = (
                _text(previous["dimension_type"]),
                payload["dimension_type"],
            )
            if old_type is not None and new_type is not None:
                _check_same(
                    "Dimension",
                    key,
                    row,
                    {"dimension_type": new_type},
                    previous,
                    {"dimension_type": old_type},
                    formal=True,
                )
        else:
            _prepare_new(prepared_dims, key, row, payload, "Dimension")
    available_keys = dim_ids.keys() | prepared_dims.keys()

    dataset_keys, dataset_identity, dataset_domains = await _caliber_dataset_maps(
        db, datasource_id, tid, wid
    )
    existing_bindings = await _formal_index(
        db,
        dataset_dimension,
        ("dataset_id", "dimension_id"),
        tid,
        wid,
        dataset_dimension.c.dataset_id.in_(dataset_domains),
    )
    cal_rows = await staging_repo.list_all(
        db,
        "staging_dataset_dimension",
        datasource_id,
        tid,
        wid,
        statuses=(ROW_CONFIRMED,),
    )
    prepared_bindings = {}
    for row in cal_rows:
        name = _need_name(row["dimension_name"], f"维度口径 staging id={row['id']} ")
        label = _text(row["dataset_name"]) or f"id={row['dataset_id']}"
        what = f"维度口径 {name!r} @ {label!r} staging id={row['id']}"
        domain_id = _resolve_domain_id(row, domain_names, what)
        dimension_key = (domain_id, name)
        if dimension_key not in available_keys:
            raise BadRequestError(
                f"{what} 无法解析维度实体（domain_id={domain_id!r}），"
                "请确认 DIMENSION_MANAGE 候选名称与口径一致"
            )
        dataset_id = _resolve_caliber_dataset_id(
            row, datasource_id, domain_id, dataset_keys, dataset_identity
        )
        if dataset_id is None:
            raise BadRequestError(
                f"{what} 无法在当前数据源 {datasource_id!r} 的 domain_id={domain_id!r} "
                f"下解析 Dataset；提供的 dataset_id={row['dataset_id']!r} 不可信或数据集"
                "尚未晋升，请修正引用或先完成 DATASET_FIELDS 阶段"
            )
        payload = _payload(row, "expr", "value_example")
        if payload["expr"] is None:
            raise BadRequestError(f"{what} 缺少 expr（口径表达式），请补齐后再完成")
        # 未插入的 Dimension 用域内 canonical key 代替 ID；写入后是一一映射。
        key = (dataset_id, dimension_key)
        previous = (
            existing_bindings.get((dataset_id, dim_ids[dimension_key]))
            if dimension_key in dim_ids
            else None
        )
        if previous is not None:
            _check_same(
                what,
                key,
                row,
                payload,
                previous,
                _payload(previous, "expr", "value_example"),
                formal=True,
            )
        else:
            _prepare_new(prepared_bindings, key, row, payload, what)

    for key, (_, payload) in prepared_dims.items():
        created = await dimension_service.create(
            db, DimensionCreate(**payload), principal
        )
        dim_ids[key] = created.dimension_id
    for (dataset_id, dimension_key), (_, payload) in prepared_bindings.items():
        await dataset_dimension_service.create(
            db,
            DatasetDimensionCreate(
                dataset_id=dataset_id,
                dimension_id=dim_ids[dimension_key],
                **payload,
            ),
            principal,
        )
    await _set_completed(db, datasource_id, stage, principal)
    return {
        "stage": stage,
        "status": STATUS_COMPLETED,
        "promoted_dimension": len(prepared_dims),
        "promoted_dataset_dimension": len(prepared_bindings),
    }


async def _complete_metric_manage(db, datasource_id, stage, principal) -> dict:
    tid, wid = principal.tenant_id, principal.active_workspace_id
    domain_names = await _domain_name_to_id(db, tid, wid)
    existing = await _formal_index(
        db, metric_dict, ("domain_id", "metric_name"), tid, wid
    )
    metric_ids = {key: row["id"] for key, row in existing.items()}
    rows = await staging_repo.list_all(
        db,
        "staging_metric",
        datasource_id,
        tid,
        wid,
        statuses=("PENDING", ROW_CONFIRMED),
    )
    prepared_metrics = {}
    for row in rows:
        name = _need_name(row["metric_name"], f"Metric staging id={row['id']} ")
        domain_id = _resolve_domain_id(row, domain_names, f"指标 {name!r}")
        key = (domain_id, name)
        payload = dict(
            domain_id=domain_id,
            metric_name=name,
            **_payload(
                row,
                "description",
                "synonyms",
                priority="MEDIUM",
            ),
        )
        if key not in existing:
            _prepare_new(prepared_metrics, key, row, payload, "Metric")
    available_keys = metric_ids.keys() | prepared_metrics.keys()

    dataset_keys, dataset_identity, dataset_domains = await _caliber_dataset_maps(
        db, datasource_id, tid, wid
    )
    existing_calibers = await _formal_index(
        db,
        metric_caliber,
        ("metric_id", "dataset_id", "time_window"),
        tid,
        wid,
        metric_caliber.c.dataset_id.in_(dataset_domains),
    )
    cal_rows = await staging_repo.list_all(
        db, "staging_metric_caliber", datasource_id, tid, wid, statuses=(ROW_CONFIRMED,)
    )
    prepared_calibers = {}
    for row in cal_rows:
        name = _need_name(row["metric_name"], f"指标口径 staging id={row['id']} ")
        label = _text(row["dataset_name"]) or f"id={row['dataset_id']}"
        what = f"指标口径 {name!r} @ {label!r} staging id={row['id']}"
        domain_id = _resolve_domain_id(row, domain_names, what)
        metric_key = (domain_id, name)
        if metric_key not in available_keys:
            raise BadRequestError(
                f"{what} 无法解析指标实体（domain_id={domain_id!r}），"
                "请确认 METRIC_MANAGE 候选名称与口径一致"
            )
        dataset_id = _resolve_caliber_dataset_id(
            row, datasource_id, domain_id, dataset_keys, dataset_identity
        )
        if dataset_id is None:
            raise BadRequestError(
                f"{what} 无法在当前数据源 {datasource_id!r} 的 domain_id={domain_id!r} "
                f"下解析 Dataset；提供的 dataset_id={row['dataset_id']!r} 不可信或数据集"
                "尚未晋升，请修正引用或先完成 DATASET_FIELDS 阶段"
            )
        payload = _payload(row, "expr", "time_window", "additivity")
        if payload["expr"] is None or payload["time_window"] is None:
            raise BadRequestError(f"{what} 缺少 expr 或 time_window，请补齐后再完成")
        time_window = payload["time_window"]
        key = (metric_key, dataset_id, time_window)
        previous = (
            existing_calibers.get((metric_ids[metric_key], dataset_id, time_window))
            if metric_key in metric_ids
            else None
        )
        if previous is not None:
            _check_same(
                what,
                key,
                row,
                payload,
                previous,
                _payload(previous, "expr", "time_window", "additivity"),
                formal=True,
            )
        else:
            _prepare_new(prepared_calibers, key, row, payload, what)

    for key, (_, payload) in prepared_metrics.items():
        created = await metric_lib_service.create(
            db, MetricCreate(**payload), principal
        )
        metric_ids[key] = created.metric_id
    for (metric_key, dataset_id, _), (_, payload) in prepared_calibers.items():
        await metric_caliber_service.create(
            db,
            CaliberCreate(
                metric_id=metric_ids[metric_key], dataset_id=dataset_id, **payload
            ),
            principal,
        )
    await _set_completed(db, datasource_id, stage, principal)
    return {
        "stage": stage,
        "status": STATUS_COMPLETED,
        "promoted_metric": len(prepared_metrics),
        "promoted_metric_caliber": len(prepared_calibers),
    }
