"""algo 的 SQL 执行统一入口：对外只有一个函数，产物满足契约::

    async execute_sql(sql, *, max_rows=None) -> DataFrame

内部两条路线，出口共享上面这一个契约：

路线 A「注入」 :class:`ProjectSqlExecutor`
    ``execute_sql`` 直给 → ``execute_sql_path`` 加载。典型住户是项目
    编排层执行器（要 datasource_id、principal 走 ContextVar 鉴权），
    测试桩走的也是这条。**不做「项目/自定义」二分** —— 没有行为消费
    这个标签；适配是逐参数的（签名要求什么绑什么），契约形态的注入
    自动退化为透传。

路线 B「自建」 :class:`SqlExecutor` + 方言子类
    与项目无关，datasource_type 直接分派到方言执行器（``dialect_keys``
    自注册，与 inputs 同一习语；连接键契约 ``required_keys`` /
    ``optional_keys`` 与方言实现同址）。profiling 独立测试 / 无宿主部署用。

选中规则：**有注入走 A，否则走 B**。注入字段名只说明「怎么送进来」，
不用于判定「它是谁」——「它需要什么」由签名说了算。

用法（仅 Config 应 import 本模块，见 ``AlgoConfig.load_*_execute_sql``）::

    execute_sql = default_execute_sql_func_builder(
        "sqlite", connection={"db_path": "…/chinook.sqlite"}
    )
    frame = await execute_sql("SELECT 1", max_rows=10)
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import sqlite3
from abc import ABC, abstractmethod
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, ClassVar, Mapping, Protocol, Sequence

import pandas as pd


class ExecuteSql(Protocol):
    """统一执行契约：协程、两参数、返回 DataFrame。"""

    async def __call__(
        self,
        sql: str,
        *,
        max_rows: int | None = None,
    ) -> pd.DataFrame: ...


# ===========================================================================
# 归一
# ===========================================================================


def _normalize(result: Any) -> pd.DataFrame:
    if isinstance(result, pd.DataFrame):
        return result
    error = getattr(result, "error", None)
    if error:
        raise RuntimeError(str(error))
    rows = getattr(result, "rows", None)
    columns = getattr(result, "columns", None)
    if rows is None or columns is None:
        raise TypeError("执行器必须返回 DataFrame 或带 rows/columns/error 的对象")
    return pd.DataFrame(rows, columns=columns)


def _empty(value: Any) -> bool:
    """绑定期「缺值」：None / 空串。空 mapping 留给连接校验自己处理。"""
    return value is None or value == ""


def _normalize_connection(connection: Any) -> Mapping[str, Any] | None:
    if connection is None:
        return None
    if not isinstance(connection, Mapping):
        raise TypeError(f"connection 须为 mapping，收到 {type(connection).__name__}")
    return None if not connection else connection


# ===========================================================================
# 路线 B「自建」：方言自注册
# ===========================================================================


class SqlExecutor(ABC):
    """自建执行器：实例即契约（``async __call__``）。

    连接键契约（``required_keys`` / ``optional_keys``）与方言实现同址；
    :meth:`create` 是分派唯一入口，未注册抛 ValueError（带已支持清单与
    注入路线的指引）—— 自建路线是硬依赖，不降级。
    """

    #: 本实现认领的 datasource_type 键（小写）。最多两个：全称 + 简称。
    dialect_keys: ClassVar[tuple[str, ...]] = ()
    required_keys: ClassVar[tuple[str, ...]] = ()
    optional_keys: ClassVar[tuple[str, ...]] = ()

    #: 键 → 实现类。子类定义时由 ``__init_subclass__`` 自动填充。
    _registry: ClassVar[dict[str, type["SqlExecutor"]]] = {}

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        for key in cls.dialect_keys:
            claimed = SqlExecutor._registry.get(key)
            if claimed is not None and claimed is not cls:
                raise TypeError(f"方言键 {key!r} 已被 {claimed.__name__} 认领")
            SqlExecutor._registry[key] = cls

    @classmethod
    def supported(cls) -> frozenset[str]:
        """已认领的方言键（结构守卫与错误消息用的公开视图）。"""
        return frozenset(SqlExecutor._registry)

    @classmethod
    def create(
        cls,
        datasource_type: str,
        connection: Mapping[str, Any] | None = None,
        *,
        max_rows_floor: int | None = None,
    ) -> "SqlExecutor":
        """按 datasource_type 分派构造。**分派的唯一入口。**"""
        impl = SqlExecutor._registry.get((datasource_type or "").strip().lower())
        if impl is None:
            raise ValueError(
                f"datasource_type {datasource_type!r} 没有自建执行器"
                f"（已支持: {', '.join(sorted(SqlExecutor._registry))}；"
                "或经 execute_sql / execute_sql_path 注入项目执行器）"
            )
        return impl(connection, max_rows_floor=max_rows_floor)

    def __init__(
        self,
        connection: Mapping[str, Any] | None = None,
        *,
        max_rows_floor: int | None = None,
    ) -> None:
        self.conn = self._pick_connection(connection)
        self._floor = max_rows_floor

    def _pick_connection(self, connection: Mapping[str, Any] | None) -> dict[str, Any]:
        conn = dict(connection or {})
        missing = [key for key in self.required_keys if not conn.get(key)]
        if missing:
            raise ValueError(
                f"{self.dialect_keys[0]} 执行器缺少连接参数 {missing}"
                f"（connection 现有键: {sorted(conn) or '无'}）；"
                "或改用 execute_sql_path 注入现成执行器"
            )
        return {
            key: conn[key]
            for key in (*self.required_keys, *self.optional_keys)
            if key in conn and conn[key] is not None
        }

    async def __call__(self, sql: str, *, max_rows: int | None = None) -> pd.DataFrame:
        """契约即签名 —— 不需要 inspect。同步 DB 调用统一下线程。"""
        limit = max_rows if max_rows is not None else self._floor
        columns, rows = await asyncio.to_thread(
            self.fetch, sql, None if limit is None else int(limit)
        )
        return pd.DataFrame(rows, columns=list(columns))

    @abstractmethod
    def fetch(
        self, sql: str, max_rows: int | None
    ) -> tuple[Sequence[str], Sequence[Any]]:
        """同步查询，返回 ``(columns, rows)``。下线程与组装由基类负责。"""


class DbApiExecutor(SqlExecutor):
    """DB-API 2.0 模板：``connect()`` → cursor → fetch。不认领键。"""

    def fetch(self, sql: str, max_rows: int | None) -> tuple[list[str], Sequence[Any]]:
        with closing(self.connect()) as conn, closing(conn.cursor()) as cursor:
            cursor.execute(sql)
            columns = [d[0] for d in cursor.description or []]
            rows = cursor.fetchall() if max_rows is None else cursor.fetchmany(max_rows)
        return columns, rows

    @abstractmethod
    def connect(self) -> Any: ...


class SqliteExecutor(DbApiExecutor):
    dialect_keys = ("sqlite",)
    required_keys = ("db_path",)

    def __init__(
        self,
        connection: Mapping[str, Any] | None = None,
        *,
        max_rows_floor: int | None = None,
    ) -> None:
        super().__init__(connection, max_rows_floor=max_rows_floor)
        db_path = Path(self.conn["db_path"])
        if not db_path.is_file():
            raise FileNotFoundError(f"sqlite 数据库文件不存在: {db_path}")
        self._uri = db_path.resolve().as_uri() + "?mode=ro"  # 只读

    def connect(self) -> Any:
        return sqlite3.connect(self._uri, uri=True)


class PostgresExecutor(DbApiExecutor):
    dialect_keys = ("postgresql", "pg")
    required_keys = ("host", "dbname", "user")
    optional_keys = ("port", "password", "connect_timeout", "sslmode")

    def connect(self) -> Any:
        import psycopg

        return psycopg.connect(autocommit=True, **{"connect_timeout": 10, **self.conn})


class BigQueryExecutor(SqlExecutor):
    dialect_keys = ("bigquery", "bq")
    optional_keys = ("credential_path", "project")

    def __init__(
        self,
        connection: Mapping[str, Any] | None = None,
        *,
        max_rows_floor: int | None = None,
    ) -> None:
        super().__init__(connection, max_rows_floor=max_rows_floor)
        credential_path = self.conn.get("credential_path")
        if credential_path and not Path(str(credential_path)).is_file():
            raise FileNotFoundError(f"bigquery 凭证文件不存在: {credential_path}")

        from google.cloud import bigquery

        if credential_path:
            from google.oauth2 import service_account

            credentials = service_account.Credentials.from_service_account_file(
                str(credential_path)
            )
            self._client = bigquery.Client(
                credentials=credentials,
                project=self.conn.get("project") or credentials.project_id,
            )
        else:
            self._client = bigquery.Client(project=self.conn.get("project"))

    def fetch(self, sql: str, max_rows: int | None) -> tuple[list[str], list[tuple]]:
        rows = self._client.query(sql).result(max_results=max_rows)
        return [field.name for field in rows.schema], [tuple(row) for row in rows]


# ===========================================================================
# 路线 A「注入」：逐参数适配
# ===========================================================================


def _load_by_path(path: str) -> Any:
    module_name, _, attr = path.partition(":")
    if not module_name or not attr:
        raise ValueError(f"非法执行器路径 {path!r}，应为 '模块:属性'")
    return getattr(importlib.import_module(module_name), attr)


#: 调用期参数：不进绑定期 pool 校验
_CALL_TIME = frozenset({"sql", "max_rows"})

#: 不参与签名绑参的 pool 键（连接 / 身份环境）
_NON_BIND_KEYS = frozenset(
    {
        "connection",
        "datasource_principal",
        "principal",
        "max_rows_floor",
        "execute_sql",
        "execute_sql_func",
        "execute_sql_path",
    }
)


def _bound_kwargs(
    raw: Callable[..., Any],
    pool: Mapping[str, Any],
) -> dict[str, Any]:
    """按 raw 形参从 pool 取绑定期参数；缺无默认值的必填则报错。"""
    try:
        signature = inspect.signature(raw)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"无法 inspect 执行器 {raw!r}: {exc}") from exc

    bound: dict[str, Any] = {}
    missing: list[str] = []
    provided = sorted(
        k for k in pool if k not in _NON_BIND_KEYS and not _empty(pool.get(k))
    )

    for name, param in signature.parameters.items():
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        if name in _CALL_TIME:
            continue

        if name in pool and not _empty(pool[name]):
            bound[name] = pool[name]
            continue

        if param.default is not inspect.Parameter.empty:
            continue  # 有默认值且 pool 未给 → 调用时省略，用 raw 默认

        missing.append(name)

    if missing:
        label = getattr(raw, "__qualname__", None) or getattr(
            raw, "__name__", repr(raw)
        )
        raise ValueError(
            f"执行器 {label} 需要参数 {missing}，但 kwargs 中缺失"
            f"（已提供: {provided or '无'}）"
        )
    return bound


class ProjectSqlExecutor:
    """注入 callable 的适配壳（典型住户：项目编排层执行器）。

    **不做「项目/自定义」二分** —— 没有行为消费这个标签。适配逐参数：
    签名要求什么就从 pool 绑什么（缺则构造期报错，双清单）、接受
    max_rows 才给 floor、给了 principal 才包 ContextVar、协程 await
    否则下线程。契约形态的注入因此自动退化为透传 + 归一。
    """

    def __init__(self, raw: Callable[..., Any], *, pool: Mapping[str, Any]) -> None:
        self._raw = raw
        self._bound = _bound_kwargs(raw, pool)  # inspect 只做构造期一次
        try:
            self._accepts_max_rows = "max_rows" in inspect.signature(raw).parameters
        except (TypeError, ValueError):
            self._accepts_max_rows = True
        self._is_coro = asyncio.iscoroutinefunction(raw)
        self._floor = pool.get("max_rows_floor")
        self._principal = pool.get("datasource_principal", pool.get("principal"))
        # 扩展点：将来若出现签名推不出的行为差异（重试 / 审计 / 计费），
        # 在这里加显式声明字段，而不是回去按注入字段猜路线

    @classmethod
    def create(
        cls,
        *,
        execute_sql: Callable[..., Any] | None = None,
        execute_sql_path: str = "",
        **pool: Any,
    ) -> "ProjectSqlExecutor":
        """func 直给 → path 加载。无分派（与 ParsedQueryLogInput.create 同构）。"""
        raw = (
            execute_sql if execute_sql is not None else _load_by_path(execute_sql_path)
        )
        return cls(raw, pool=pool)

    async def __call__(self, sql: str, *, max_rows: int | None = None) -> pd.DataFrame:
        call_kw = dict(self._bound)
        if self._accepts_max_rows:
            limit = max_rows if max_rows is not None else self._floor
            if limit is not None:
                call_kw["max_rows"] = int(limit)

        ctx = self._principal_ctx()
        if ctx is None:
            return await self._invoke(sql, call_kw)
        token = ctx.set(self._principal)
        try:
            return await self._invoke(sql, call_kw)
        finally:
            ctx.reset(token)

    async def _invoke(self, sql: str, call_kw: dict[str, Any]) -> pd.DataFrame:
        if self._is_coro:
            result = await self._raw(sql, **call_kw)
        else:
            result = await asyncio.to_thread(self._raw, sql, **call_kw)
        return _normalize(result)

    def _principal_ctx(self) -> Any | None:
        """B0：编排层靠 ContextVar 取身份；独立 algo 部署无此模块则跳过。"""
        if self._principal is None:
            return None
        try:
            from context_manager.api.auth_middleware import principal_ctx
        except ImportError:  # pragma: no cover - 独立 algo 部署
            return None
        return principal_ctx

    def __repr__(self) -> str:
        label = getattr(self._raw, "__qualname__", None) or repr(self._raw)
        return (
            f"ProjectSqlExecutor({label}, bound={sorted(self._bound)}, "
            f"principal={'有' if self._principal is not None else '无'})"
        )


# ===========================================================================
# 唯一公开函数
# ===========================================================================


def default_execute_sql_func_builder(
    datasource_type: str,
    *,
    execute_sql: Callable[..., Any] | None = None,
    execute_sql_path: str = "",
    **kwargs: Any,
) -> ExecuteSql:
    """两条路线的唯一分派点：**有注入走 A（适配），否则走 B（自建）**。

    ``kwargs`` 常见键：``datasource_id`` / ``max_rows_floor`` /
    ``connection`` / ``principal``（``datasource_principal`` 同义）。
    """
    if execute_sql is None and kwargs.get("execute_sql_func") is not None:
        execute_sql = kwargs["execute_sql_func"]  # 兼容旧别名

    connection = _normalize_connection(kwargs.get("connection"))
    path = (execute_sql_path or "").strip()

    if execute_sql is None and not path:
        return SqlExecutor.create(
            datasource_type,
            connection,
            max_rows_floor=kwargs.get("max_rows_floor"),
        )

    pool = dict(kwargs)
    if connection is not None:
        pool["connection"] = connection
    return ProjectSqlExecutor.create(
        execute_sql=execute_sql, execute_sql_path=path, **pool
    )


__all__ = [
    "ExecuteSql",
    "ProjectSqlExecutor",
    "SqlExecutor",
    "default_execute_sql_func_builder",
]
