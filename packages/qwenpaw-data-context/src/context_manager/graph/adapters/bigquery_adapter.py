"""BigQuery adapter — direct google-cloud-bigquery port implementation, not a
UniversalConnector subclass.

选择 SDK 直连而非 sqlalchemy-bigquery dialect 的原因：

- 跨项目公共数据集：``schemas`` 每项支持 ``project.dataset`` 限定（计费
  project ≠ 数据 project）；
- 嵌套 RECORD/STRUCT 字段展平成 ``event_params.key`` 子列，元数据对
  text-to-SQL 可用；
- 分区键取自 ``time_partitioning`` / ``range_partitioning``，不靠列名猜测；
- 费用护栏：``maximum_bytes_billed``；不做 ``SELECT DISTINCT`` 采样
 （整列扫描计费）。

google-cloud-bigquery 是可选依赖（``qwenpaw-data-context[bigquery]``），
仅在实际使用时导入。
"""
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, Iterator, Optional, Sequence

from pydantic import BaseModel

from ...secrets.schemas import BigQueryConnection
from ...utils import get_logger
from ..keys import column_key, derive_layer, table_key
from ..physical import ColumnRecord, TableRecord
from .base import (
    BaseConnector,
    ConnectionTestResult,
    ConnectorError,
    ExecResult,
    PhysicalManifest,
)

if TYPE_CHECKING:
    from ...contracts.import_models import SourceConfig

log = get_logger("graph.adapters.bigquery")

#: Max datasets listed during test_connection (reachability only).
_TEST_LIST_LIMIT = 10

#: BigQuery legacy type name → GoogleSQL name; complex types keep lowercase raw form.
_BQ_TYPE_RENAMES = {
    "integer": "int64",
    "float": "float64",
    "boolean": "bool",
    "record": "struct",
}


def _normalize_bq_type(field: Any) -> str:
    t = str(getattr(field, "field_type", "") or "string").strip().lower()
    t = _BQ_TYPE_RENAMES.get(t, t)
    if str(getattr(field, "mode", "") or "").upper() == "REPEATED":
        return f"array<{t}>"
    return t


def _walk_fields(fields: Sequence[Any], prefix: str = "") -> Iterator[tuple[str, Any]]:
    """DFS 展平嵌套字段：RECORD 本身与其子字段都产出。"""
    for f in fields:
        name = f"{prefix}{f.name}"
        yield name, f
        if str(getattr(f, "field_type", "")).upper() in ("RECORD", "STRUCT"):
            yield from _walk_fields(list(f.fields), prefix=f"{name}.")


class BigQueryConnector(BaseConnector):
    """BigQuery adapter: extract_metadata / execute_sql / test_connection via
    google-cloud-bigquery."""

    connection_model: type[BaseModel] = BigQueryConnection

    def __init__(self, conn: BaseModel, *, db_id: str):
        super().__init__(db_id=db_id)
        self._conn = conn
        self._client = self._create_client()

    # ---- Construction entries ----
    @classmethod
    def from_config(cls, config: "SourceConfig", db_id: str) -> "BigQueryConnector":
        conn = getattr(config, "connection", None)
        if not isinstance(conn, cls.connection_model):
            raise ConnectorError(
                f"{cls.__name__} 需要 {cls.connection_model.__name__} 连接配置，"
                f"got {type(conn).__name__}"
            )
        return cls(conn, db_id=db_id)

    @classmethod
    def from_connection(cls, conn: BaseModel, db_id: str) -> "BigQueryConnector":
        if not isinstance(conn, cls.connection_model):
            raise ConnectorError(
                f"{cls.__name__} 需要 {cls.connection_model.__name__} 连接配置，"
                f"got {type(conn).__name__}"
            )
        return cls(conn, db_id=db_id)

    # ---- Client construction ----
    def _create_client(self):
        try:
            from google.cloud import bigquery
            from google.oauth2 import service_account
        except ImportError as exc:
            raise ConnectorError(
                "bigquery 依赖 google-cloud-bigquery，当前环境未安装；"
                '请先安装（pip install "qwenpaw-data-context[bigquery]"）',
                cause=exc,
            ) from exc
        conn = self._conn
        try:
            info = json.loads(conn.service_account_json.get_secret_value())
            creds = service_account.Credentials.from_service_account_info(info)
            return bigquery.Client(
                project=conn.project_id,
                credentials=creds,
                location=conn.location or None,
            )
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(
                f"BigQuery 客户端构造失败（project={conn.project_id!r}）: {exc}",
                cause=exc,
            ) from exc

    def close(self) -> None:
        try:
            self._client.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("bigquery client close failed: %s", exc)

    # ---- Port: test_connection (never raises) ----
    def test_connection(self) -> ConnectionTestResult:
        try:
            n = 0
            for _ in self._client.list_datasets(max_results=_TEST_LIST_LIMIT):
                n += 1
            return ConnectionTestResult(
                success=True,
                message=f"connected to BigQuery project {self._conn.project_id}",
                tables_found=n,
            )
        except Exception as exc:  # noqa: BLE001 — 连接失败是预期结果，不抛
            return ConnectionTestResult(success=False, message=str(exc))

    # ---- Port: execute_sql (never raises) ----
    def execute_sql(self, sql: str, *, max_rows: int = 200) -> ExecResult:
        t0 = time.time()
        job = None
        try:
            from google.cloud import bigquery

            job_config = bigquery.QueryJobConfig(use_legacy_sql=False)
            if getattr(self._conn, "maximum_bytes_billed", None):
                job_config.maximum_bytes_billed = int(
                    self._conn.maximum_bytes_billed
                )
            job = self._client.query(sql, job_config=job_config)
            result = job.result(max_results=max_rows)
            cols = [str(f.name) for f in result.schema]
            rows = [list(row) for row in result]
            total = result.total_rows
            truncated = bool(total is not None and total > max_rows)
            return ExecResult(
                sql=sql,
                columns=cols,
                rows=rows,
                row_count=int(total) if total is not None else len(rows),
                truncated=truncated,
                elapsed_ms=(time.time() - t0) * 1000,
                instance_id=job.job_id,
                task_status=str(job.state),
            )
        except Exception as exc:  # noqa: BLE001 — SQL failure is expected
            return ExecResult(
                sql=sql,
                error=str(exc),
                elapsed_ms=(time.time() - t0) * 1000,
                instance_id=getattr(job, "job_id", None),
            )

    # ---- Port: extract_metadata (raises ConnectorError) ----
    def extract_metadata(self, schemas: Sequence[str]) -> PhysicalManifest:
        db = self._db_id
        refs = [s.strip() for s in schemas if s and s.strip()]
        table_recs: list[TableRecord] = []
        col_recs: list[ColumnRecord] = []
        label = "default"
        try:
            if not refs:
                refs = [d.dataset_id for d in self._client.list_datasets()]
            for i, ref in enumerate(refs):
                ds_label = self._reflect_dataset(ref, db, table_recs, col_recs)
                if i == 0:
                    label = ds_label
        except ConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001 — wrap into the layer exception
            raise ConnectorError(
                f"反射 BigQuery 数据源 {db!r} 元数据失败（datasets={refs!r}）: {exc}",
                cause=exc,
            ) from exc

        log.info(
            "BigQuery reflected: db=%s datasets=%s tables=%d columns=%d",
            db, refs, len(table_recs), len(col_recs),
        )
        return PhysicalManifest(
            db_id=db, schema=label, tables=table_recs, columns=col_recs, fks=[],
        )

    # ---- Reflection steps ----
    def _dataset_ref(self, ref: str):
        """``dataset`` 或 ``project.dataset``（GCP project id 不含点，安全按
        首个点切分）。"""
        from google.cloud import bigquery

        if "." in ref:
            project, dataset = ref.split(".", 1)
        else:
            project, dataset = self._conn.project_id, ref
        return bigquery.DatasetReference(project, dataset)

    def _reflect_dataset(
        self,
        ref: str,
        db: str,
        table_recs: list[TableRecord],
        col_recs: list[ColumnRecord],
    ) -> str:
        ds_ref = self._dataset_ref(ref)
        # 图 key 保持三段式（db.schema.table）；project 限定保留在 DDL 全名里。
        schema_label = ds_ref.dataset_id
        for item in self._client.list_tables(ds_ref):
            table = self._client.get_table(item.reference)
            partition_key = self._partition_key(table)
            table_recs.append(
                TableRecord(
                    key=table_key(db, schema_label, table.table_id),
                    db=db,
                    schema=schema_label,
                    name=table.table_id,
                    layer=derive_layer(table.table_id),
                    partition_key=partition_key,
                    comment=(table.description or "").strip(),
                    ddl=self._table_ddl(table),
                )
            )
            for name, f in _walk_fields(list(table.schema)):
                ctype = _normalize_bq_type(f)
                ccomment = (getattr(f, "description", None) or "").strip()
                text = f"{db}.{table.table_id}.{name} ({ctype})"
                if ccomment:
                    text += f" — {ccomment}"
                col_recs.append(
                    ColumnRecord(
                        key=column_key(db, schema_label, table.table_id, name),
                        db=db,
                        schema=schema_label,
                        table=table.table_id,
                        name=name,
                        type=ctype,
                        pk=False,
                        nullable=str(
                            getattr(f, "mode", "") or "NULLABLE"
                        ).upper() != "REQUIRED",
                        is_partition=(name == partition_key),
                        comment=ccomment,
                        description=ccomment,
                        text=text,
                    )
                )
        return schema_label

    @staticmethod
    def _partition_key(table: Any) -> Optional[str]:
        tp = getattr(table, "time_partitioning", None)
        if tp is not None:
            return tp.field or "_PARTITIONTIME"  # ingestion-time 分区的伪列
        rp = getattr(table, "range_partitioning", None)
        if rp is not None:
            return rp.field
        return None

    @staticmethod
    def _table_ddl(table: Any) -> str:
        """View 用原始查询；表合成展平列清单的 CREATE TABLE（提示语料，非可执行 DDL）。"""
        if str(getattr(table, "table_type", "")) in ("VIEW", "MATERIALIZED_VIEW"):
            body = (
                getattr(table, "view_query", None)
                or getattr(table, "mview_query", None)
                or ""
            ).strip()
            return body.rstrip(";") + ";" if body else ""
        flat = list(_walk_fields(list(table.schema)))
        if not flat:
            return ""
        lines = [f"CREATE TABLE `{table.reference}` ("]
        for i, (name, f) in enumerate(flat):
            suffix = "," if i < len(flat) - 1 else ""
            nn = (
                " NOT NULL"
                if str(getattr(f, "mode", "") or "").upper() == "REQUIRED"
                else ""
            )
            lines.append(f"  `{name}` {_normalize_bq_type(f)}{nn}{suffix}")
        lines.append(");")
        return "\n".join(lines)


__all__ = ["BigQueryConnector"]
