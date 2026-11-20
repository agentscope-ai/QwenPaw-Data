"""单条 SQL 的解析、作用域索引与物理表血缘查询。

缓存与错误处理约定：

- 删 pickle 协议（``__getstate__``/``__setstate__``）——本架构**不落 AST 缓存**，
  缓存成果（证据 DataFrame），不缓存中间态；
- 删 ``create_from_sql_or_none``——解析失败要进 ``failed_statements`` 留给人看，
  由 ``parse_evidence`` 的主循环 try/except 捕获原因，不做静默吞失败。

本模块只公开 :class:`SqlAnalysis`。四个 parser 通过同一个对象读取一条 SQL 的
AST 信息（穿透 CTE / 派生表 / UNION 的物理表血缘），不再自行构建 CTE map 或
alias map。业务判定仍由各 parser 负责。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TypeAlias

import sqlglot
from sqlglot import exp

__all__ = ["SqlAnalysis"]


_SET_OPERATIONS = (exp.Union, exp.Intersect, exp.Except)
_QUERY_EXPRESSIONS = (exp.Select, *_SET_OPERATIONS)
_QueryExpression: TypeAlias = exp.Select | exp.Union | exp.Intersect | exp.Except
_ResolvedSelectSource: TypeAlias = tuple[exp.Expression, frozenset[str]]
_MAX_PARENT_DEPTH = 256
_MAX_UNWRAP_DEPTH = 64


@dataclass(frozen=True)
class _SelectScope:
    """
    一个 Select 的只读来源索引。
    aliases: 映射 alias -> 物理表集合；不区分大小写；
        空集合表示 alias 不存在。
    source_tables: Select 的 FROM/JOIN 穿透到的物理表集合。

    下游任务：
        由 ``SqlAnalysis.resolve_select_source_table_names``、
        ``resolve_column_source_table_names`` 和
        ``resolve_unique_column_source_table_name`` 读取，最终服务于维度抽取、指标抽取和
        Biz-domain JOIN 边抽取；profile 模块不会直接构造或导入本类。

    完整示例：

    输入 SQL::

        SELECT o.user_id, u.name
        FROM dwd.orders AS o
        JOIN dwd.users AS u ON o.user_id = u.id

    构建后的完整输出::

        _SelectScope(
            aliases={
                "o": frozenset({"dwd.orders"}),
                "u": frozenset({"dwd.users"}),
            },
            source_tables=frozenset({"dwd.orders", "dwd.users"}),
        )

    Corner cases：
        - ``FROM (SELECT * FROM dwd.orders) AS o`` 会得到
          ``o -> frozenset({"dwd.orders"})``。
        - alias 指向 UNION 子查询时，集合可以包含多张物理表。
        - Select 没有 FROM 时，aliases 为空且 source_tables 为 frozenset()。
    """

    aliases: Mapping[str, frozenset[str]]
    source_tables: frozenset[str]


@dataclass(frozen=True)
class _AnalysisIndexes:
    """一条 SQL 的全部只读索引。

    ``selects`` 和 ``joins`` 保存稳定顺序的 AST 节点；``physical_tables`` 是
    整条 SQL 穿透 CTE/子查询后的物理表并集。后两个 Mapping 使用 AST 对象的
    ``id(select)`` 寻址，只在当前 :class:`SqlAnalysis` 生命周期内有效。

    下游任务：
        由 ``SqlAnalysis`` 的全部查询属性和方法读取，最终共同服务于 Dataset、
        Dimension、Metric 和 Biz-domain 四类 profile 任务；profile 不直接使用。

    完整示例输入和输出::

        root = sqlglot.parse_one(
            "WITH paid AS (SELECT * FROM dwd.orders) "
            "SELECT p.id FROM paid AS p "
            "JOIN dim.users AS u ON p.user_id = u.id"
        )
        indexes = _build_analysis_indexes(root)
        outer_select = indexes.selects[0]
        tuple(select.sql() for select in indexes.selects)
        # (
        #   "WITH paid AS (SELECT * FROM dwd.orders) SELECT p.id FROM paid AS p "
        #   "JOIN dim.users AS u ON p.user_id = u.id",
        #   "SELECT * FROM dwd.orders",
        # )
        tuple(join.sql() for join in indexes.joins)
        # ("JOIN dim.users AS u ON p.user_id = u.id",)
        indexes.physical_tables
        # frozenset({"dwd.orders", "dim.users"})
        dict(indexes.visible_ctes_by_select_id[id(outer_select)])
        # {"paid": frozenset({"dwd.orders"})}
        dict(indexes.scopes_by_select_id[id(outer_select)].aliases)
        # {
        #   "p": frozenset({"dwd.orders"}),
        #   "u": frozenset({"dim.users"}),
        # }

    Corner cases：
        - 没有 Select 的 AST 会生成空 selects/joins/scopes。
        - 嵌套同名 CTE 分别记录在对应 Select id 下，不共享一份全局 CTE map。
        - UNION 的两侧 Select 都进入 selects，physical_tables 是两侧表的并集。
    """

    selects: tuple[exp.Select, ...]
    joins: tuple[exp.Join, ...]
    physical_tables: frozenset[str]
    visible_ctes_by_select_id: Mapping[int, Mapping[str, frozenset[str]]]
    scopes_by_select_id: Mapping[int, _SelectScope]


@dataclass(frozen=True)
class _AnalysisFragment:
    """一次 Query 或 Relation 递归分析产生的只读增量。

    ``source_table_names`` 是当前 AST 节点穿透 CTE、子查询和集合查询后读取的
    物理表。后两个 Mapping 是当前节点整棵子树中新分析出的 Select 索引；调用
    方通过返回值显式合并它们，递归函数不会修改调用方传入的字典或集合。

    下游任务：
        ``_analyze_query_expr`` 和 ``_analyze_relation_expr`` 使用本类向上返回
        数据流，最终由 ``_build_analysis_indexes`` 转换为 ``_AnalysisIndexes``。

    完整示例输入和输出::

        root = sqlglot.parse_one("SELECT * FROM dwd.orders")
        result = _analyze_query_expr(root, {}, frozenset())
        result.source_table_names
        # frozenset({"dwd.orders"})
        tuple(result.scopes_by_select_id)
        # (id(root),)

    Corner cases：
        - 无 FROM 的 Select 返回空 ``source_table_names``，但仍包含它自己的 scope。
        - 递归保护命中时三个字段都为空。
        - scalar subquery 的 scope 会进入两个 Mapping，但其表不会混入外层
          Select 的 ``source_table_names``。
    """

    source_table_names: frozenset[str]
    visible_ctes_by_select_id: Mapping[int, Mapping[str, frozenset[str]]]
    scopes_by_select_id: Mapping[int, _SelectScope]


_EMPTY_ANALYSIS_FRAGMENT = _AnalysisFragment(
    source_table_names=frozenset(),
    visible_ctes_by_select_id=MappingProxyType({}),
    scopes_by_select_id=MappingProxyType({}),
)


class SqlAnalysis:
    """保存并查询单条 SQL 的 AST 分析上下文。

    通过 :meth:`create_from_sql` 创建实例，不直接调用构造器。
    一个实例只对应一条 SQL，不跨 SQL 复用。解析失败抛 ``ValueError``，由
    ``parse_evidence`` 主循环捕获并记入 ``failed_statements``。

    下游任务：
        sql_template、dimension_usage、metric_expr 和 join 四个 parser 对同一条
        SQL 共享该上下文，分别读取物理表、列/表达式归属和 JOIN 关系。

    基本使用示例::

        analysis = SqlAnalysis.create_from_sql(
            "SELECT o.user_id FROM dwd.orders AS o",
            dialect="postgres",
        )
        cleaned_sql = analysis.cleaned_sql
        root = analysis.root_expr
        parents = sorted(analysis.physical_table_names)

    遍历 Select 和 JOIN::

        for select in analysis.select_exprs:
            tables = analysis.resolve_select_source_table_names(select)

        for join in analysis.join_exprs:
            select = analysis.resolve_owning_select_expr(join)
            if select is None:
                continue
            on_parts = analysis.extract_conjunct_exprs(join.args.get("on"))

    解析列的唯一来源表，适用于维度和 JOIN 边抽取::

        table = analysis.resolve_unique_column_source_table_name(select, column)
        if table is None:
            # 无法解析或存在多表歧义，不应猜测来源表。
            return
        dimension_name = analysis.extract_column_name(column)

    解析表达式依赖表，适用于指标抽取::

        tables = analysis.resolve_expression_source_table_names(select, metric_expression)
        table_name = analysis.convert_table_names_to_profile_table_ref(tables)

    各公开成员的用途：

    - :attr:`original_sql`：调用方传入的 SQL 原文。
    - :attr:`cleaned_sql`：移除注释和非法控制字符后的 SQL。
    - :attr:`dialect_name`：本次 SQLGlot 解析使用的 dialect。
    - :attr:`root_expr`：只读使用的 SQLGlot AST；需要改写时先 ``copy()``。
    - :attr:`select_exprs`：稳定顺序的所有 Select 节点。
    - :attr:`join_exprs`：稳定顺序的所有 Join 节点。
    - :attr:`physical_table_names`：穿透 CTE/子查询后的物理表集合。
    - :meth:`resolve_owning_select_expr`：查找表达式最近所属的 Select。
    - :meth:`resolve_select_source_table_names`：查询一个 Select 的 FROM/JOIN 来源表。
    - :meth:`resolve_column_source_table_names`：查询一个 Column 可能对应的物理表集合。
    - :meth:`resolve_unique_column_source_table_name`：只在唯一归属时返回表名。
    - :meth:`resolve_expression_source_table_names`：查询表达式依赖的物理表集合。
    - :meth:`convert_table_names_to_profile_table_ref`：在 profile 输出边界生成兼容字符串。
    - :meth:`extract_column_name`：统一取得小写列名。
    - :meth:`extract_conjunct_exprs`：只拆分顶层 AND 链，保留 OR 子树。

    内部 AST 和索引在实例创建后不应被修改。虽然 :attr:`root_expr` 必须暴露给
    profile extractor 遍历，调用方仍应把它视作只读对象。
    """

    DEFAULT_DIALECT = "postgres"

    def __init__(
        self,
        *,
        original_sql: str,
        cleaned_sql: str,
        dialect: str,
        root: exp.Expression,
        indexes: _AnalysisIndexes,
    ) -> None:
        """保存已经解析和索引完成的单 SQL 状态。

        该构造器只供 :meth:`create_from_sql` 内部使用。调用方只需传入 SQL 和 dialect，
        不应自行创建 ``_AnalysisIndexes``。

        下游任务：
            不被 profile 直接调用；它是 ``create_from_sql`` 构建 Dataset、Dimension、
            Metric 和 Biz-domain 共用上下文的最后一步。

        完整示例输入和输出::

            analysis = SqlAnalysis.create_from_sql(
                "SELECT * FROM dwd.orders",
                dialect="postgres",
            )
            isinstance(analysis, SqlAnalysis)
            # True
            analysis.original_sql
            # "SELECT * FROM dwd.orders"
            analysis.physical_table_names
            # frozenset({"dwd.orders"})

        Corner case：直接调用构造器并传入不匹配的 root/indexes 会破坏缓存一致性，
        因此外部代码禁止直接构造。
        """
        self._original_sql = original_sql
        self._cleaned_sql = cleaned_sql
        self._dialect = dialect
        self._root = root
        self._indexes = indexes

    @classmethod
    def create_from_sql(
        cls,
        sql: str,
        dialect: str = DEFAULT_DIALECT,
    ) -> SqlAnalysis:
        """解析 SQL 并构建单 SQL 分析上下文。

        参数：
            sql: SQL 原文，可以包含行注释、块注释和历史日志控制字符。
            dialect: SQLGlot dialect，例如 ``postgres`` 或 ``mysql``。

        返回：
            构建好 Select、JOIN、CTE 和物理表索引的 ``SqlAnalysis``。

        异常：
            ValueError: SQL 清洗后为空，或 SQLGlot 无法按指定 dialect 解析。

        下游任务：
            四类 profile 批量入口创建单 SQL 上下文时的严格入口；调试、测试和
            必须成功的单条 SQL 处理优先使用本方法。

        完整一般示例::

            analysis = SqlAnalysis.create_from_sql(
                "SELECT o.id FROM dwd.orders AS o -- query log comment",
                dialect="postgres",
            )
            analysis.cleaned_sql
            # "SELECT o.id FROM dwd.orders AS o"
            analysis.physical_table_names
            # frozenset({"dwd.orders"})

        Corner cases::

            SqlAnalysis.create_from_sql("   -- only comment", dialect="postgres")
            # raises ValueError("SQL is empty after cleaning")

            SqlAnalysis.create_from_sql("SELECT FROM", dialect="postgres")
            # raises ValueError("failed to parse SQL ...")

            SqlAnalysis.create_from_sql("SELECT * FROM orders").physical_table_names
            # frozenset({"orders"})  （非 CTE 的裸表直接视为物理表）

            SqlAnalysis.create_from_sql(
                "WITH x AS (SELECT 1) SELECT * FROM x"
            ).physical_table_names
            # frozenset()  （CTE 引用不会被误判为物理表）
        """
        original_sql = str(sql or "")
        cleaned_sql = _transform_raw_sql_to_clean_sql(original_sql)
        if not cleaned_sql:
            raise ValueError("SQL is empty after cleaning")

        # 先规范化 dialect，确保解析和上下文记录使用同一个名称。
        normalized_dialect = str(dialect or cls.DEFAULT_DIALECT).strip().lower()

        try:
            root = sqlglot.parse_one(cleaned_sql, read=normalized_dialect)
        except Exception as exc:
            preview = re.sub(r"\s+", " ", cleaned_sql)[:160]
            raise ValueError(
                f"failed to parse SQL with dialect={normalized_dialect!r}: "
                f"{preview!r}; cause={exc}"
            ) from exc

        # AST 只解析一次；作用域、CTE 和表血缘在构造上下文时一并预计算。
        indexes = _build_analysis_indexes(root)
        return cls(
            original_sql=original_sql,
            cleaned_sql=cleaned_sql,
            dialect=normalized_dialect,
            root=root,
            indexes=indexes,
        )

    @property
    def original_sql(self) -> str:
        """返回调用方传入的 SQL 原文，不移除注释或空白。

        下游任务：
            当前 profile 主流程不依赖该属性；它用于错误审计、日志追踪和对比
            cleaned SQL，避免丢失 query log 的原始证据。

        完整示例::

            sql = "  SELECT 1 -- note\n"
            analysis = SqlAnalysis.create_from_sql(sql, dialect="postgres")
            analysis.original_sql
            # "  SELECT 1 -- note\n"
        """
        return self._original_sql

    @property
    def cleaned_sql(self) -> str:
        """返回清洗后、实际交给 SQLGlot 的 SQL。

        下游任务：
            Dataset profile 将它写入 ``repeated_sql``，作为结构模板代表 SQL；
            其他 profile 可用于调试解析结果。

        完整一般示例::

            analysis = SqlAnalysis.create_from_sql(
                "SELECT '#1' AS code FROM dwd.orders # remove\nWHERE id = 1",
                dialect="postgres",
            )
            analysis.cleaned_sql
            # "SELECT '#1' AS code FROM dwd.orders \nWHERE id = 1"

        Corner case：引号中的 ``#1`` 被保留，真正的 ``# remove`` 行注释被删除。
        """
        return self._cleaned_sql

    @property
    def dialect_name(self) -> str:
        """返回解析使用的、已经转成小写的 SQLGlot dialect。

        下游任务：
            Dataset profile 生成规范化 SQL template，Metric profile 调用
            ``expression.sql(dialect=...)`` 输出公式。

        完整示例::

            analysis = SqlAnalysis.create_from_sql("SELECT `id` FROM dwd_orders", dialect="MYSQL")
            analysis.dialect_name
            # "mysql"
        """
        return self._dialect

    @property
    def root_expr(self) -> exp.Expression:
        """返回 SQLGlot AST 根节点。

        调用方应只读遍历；需要规范化 WHERE/HAVING 或改写投影时先复制 AST，
        否则实例内预构建的作用域索引会与 AST 不一致。

        下游任务：
            Dataset profile 复制 root 后替换筛选字面量并生成 norm key；需要特殊
            AST 节点的 extractor 也可只读遍历。

        完整示例::

            analysis = SqlAnalysis.create_from_sql(
                "SELECT id FROM dwd.orders WHERE status = 'paid'",
                dialect="postgres",
            )
            normalized_root = analysis.root_expr.copy()
            normalized_root is analysis.root_expr
            # False
            normalized_root.sql(dialect=analysis.dialect_name)
            # "SELECT id FROM dwd.orders WHERE status = 'paid'"
            analysis.root_expr.sql(dialect=analysis.dialect_name)
            # "SELECT id FROM dwd.orders WHERE status = 'paid'"

        Corner case：直接执行 ``analysis.root_expr.set(...)`` 不会自动重建 scope，
        后续 table resolution 可能过期，因此禁止原地修改。
        """
        return self._root

    @property
    def select_exprs(self) -> tuple[exp.Select, ...]:
        """返回 AST 中稳定顺序的全部 Select 节点。

        下游任务：
            Dimension profile 遍历 GROUP BY/WHERE；Metric profile 遍历投影和
            HAVING；Dataset/Biz-domain 通常不需要直接遍历此属性。

        完整一般示例::

            analysis = SqlAnalysis.create_from_sql(
                "WITH x AS (SELECT id FROM dwd.orders) SELECT id FROM x"
            )
            tuple(select.sql() for select in analysis.select_exprs)
            # (
            #   "WITH x AS (SELECT id FROM dwd.orders) SELECT id FROM x",
            #   "SELECT id FROM dwd.orders",
            # )

        Corner case::

            SqlAnalysis.create_from_sql(
                "SELECT * FROM dwd.a UNION ALL SELECT * FROM dwd.b"
            ).select_exprs
            # (left_select, right_select)
        """
        return self._indexes.selects

    @property
    def join_exprs(self) -> tuple[exp.Join, ...]:
        """返回 AST 中稳定顺序的全部 Join 节点。

        下游任务：
            Biz-domain profile 遍历它并从 ON 等值条件生成表关联边。

        完整一般示例::

            analysis = SqlAnalysis.create_from_sql(
                "SELECT * FROM dwd.orders o "
                "JOIN dim.users u ON o.user_id = u.id"
            )
            len(analysis.join_exprs)
            # 1
            analysis.join_exprs[0].sql()
            # "JOIN dim.users AS u ON o.user_id = u.id"

        Corner cases：没有 JOIN 时返回 ``()``；嵌套子查询中的 JOIN 也会出现在
        tuple 中，使用 ``resolve_owning_select_expr`` 获取它所属的 Select。
        """
        return self._indexes.joins

    @property
    def physical_table_names(self) -> frozenset[str]:
        """返回整条 SQL 穿透 CTE、子查询和集合查询后的物理表集合。

        下游任务：
            Dataset profile 把排序后的集合序列化为 ``parents``；
            它也可用于profiling 日志和解析质量统计。

        完整一般示例::

            analysis = SqlAnalysis.create_from_sql(
                "WITH x AS (SELECT * FROM dwd.a UNION ALL SELECT * FROM dwd.b) "
                "SELECT * FROM x"
            )
            analysis.physical_table_names
            # frozenset({"dwd.a", "dwd.b"})

        Corner cases::

            SqlAnalysis.create_from_sql("SELECT 1").physical_table_names
            # frozenset()

            SqlAnalysis.create_from_sql("SELECT * FROM orders").physical_table_names
            # frozenset({"orders"})

            SqlAnalysis.create_from_sql(
                "SELECT * FROM sales.orders"
            ).physical_table_names
            # frozenset({"sales.orders"})
        """
        return self._indexes.physical_tables

    def resolve_owning_select_expr(
        self, expression: exp.Expression
    ) -> exp.Select | None:
        """沿 parent 链返回表达式最近所属的 Select。

        参数：
            expression: 当前实例 AST 中的任意表达式，例如 Join、Column 或 AggFunc。

        返回：
            最近的 ``exp.Select``；表达式不属于任何 Select 时返回 ``None``。

        下游任务：
            Biz-domain profile 用它把 ``analysis.join_exprs`` 中的 Join 关联回 Select，
            再使用同一个 Select scope 解析 ON 两侧 Column。

        完整一般示例::

            analysis = SqlAnalysis.create_from_sql(
                "SELECT * FROM dwd.orders o "
                "JOIN dim.users u ON o.user_id = u.id"
            )
            join = analysis.join_exprs[0]
            select = analysis.resolve_owning_select_expr(join)
            select is analysis.select_exprs[0]
            # True

        Corner case::

            orphan = exp.column("id")
            analysis.resolve_owning_select_expr(orphan)
            # None
        """
        current: exp.Expression | None = expression
        for _ in range(_MAX_PARENT_DEPTH):
            if current is None:
                return None
            if isinstance(current, exp.Select):
                return current
            current = current.parent
        return None

    def resolve_select_source_table_names(self, select: exp.Select) -> frozenset[str]:
        """返回一个 Select 的 FROM/JOIN 穿透后的物理表集合。

        下游任务：
            Metric profile 处理 ``COUNT(*)`` 等无 Column 聚合时确定指标来源；
            ``resolve_expression_source_table_names`` 的无 Column fallback 也调用本方法。

        完整一般示例::

            analysis = SqlAnalysis.create_from_sql(
                "SELECT * "
                "FROM (SELECT * FROM dwd.orders) AS o "
                "JOIN dim.users AS u ON o.user_id = u.id"
            )
            outer_select = analysis.select_exprs[0]
            analysis.resolve_select_source_table_names(outer_select)
            # frozenset({"dwd.orders", "dim.users"})

        Corner cases::

            SqlAnalysis.create_from_sql("SELECT 1").resolve_select_source_table_names(
                SqlAnalysis.create_from_sql("SELECT 1").select_exprs[0]
            )
            # raises ValueError  （Select 属于另一个实例）

            no_from = SqlAnalysis.create_from_sql("SELECT 1")
            no_from.resolve_select_source_table_names(no_from.select_exprs[0])
            # frozenset()
        """
        return self._resolve_select_scope(select).source_tables

    def resolve_column_source_table_names(
        self,
        select: exp.Select,
        column: exp.Column,
    ) -> frozenset[str]:
        """返回 Column 在当前 Select 中可能归属的物理表集合。

        解析规则：带 qualifier 的列查 alias map；未限定列只有在 Select 唯一来源
        一张物理表时才能确定。多表 JOIN 的未限定列返回空集合，不猜测。

        下游任务：
            Metric profile 直接用它合并表达式内所有 Column 的表依赖；
            Dimension 和 Biz-domain 通过 ``resolve_unique_column_source_table_name`` 间接使用。

        完整一般示例::

            analysis = SqlAnalysis.create_from_sql(
                "SELECT o.amount, u.name "
                "FROM dwd.orders AS o "
                "JOIN dim.users AS u ON o.user_id = u.id"
            )
            select = analysis.select_exprs[0]
            columns = {column.sql(): column for column in select.find_all(exp.Column)}
            analysis.resolve_column_source_table_names(select, columns["o.amount"])
            # frozenset({"dwd.orders"})
            analysis.resolve_column_source_table_names(select, columns["u.name"])
            # frozenset({"dim.users"})

        Corner cases::

            ambiguous = SqlAnalysis.create_from_sql(
                "SELECT id FROM dwd.orders o "
                "JOIN dim.users u ON o.user_id = u.id"
            )
            select = ambiguous.select_exprs[0]
            id_column = next(select.expressions[0].find_all(exp.Column))
            ambiguous.resolve_column_source_table_names(select, id_column)
            # frozenset()  （未限定 id，当前 Select 有两张来源表）

            subquery = SqlAnalysis.create_from_sql(
                "SELECT x.id FROM ("
                "SELECT id FROM dwd.a UNION ALL SELECT id FROM dwd.b"
                ") AS x"
            )
            select = subquery.select_exprs[0]
            x_id = next(select.expressions[0].find_all(exp.Column))
            subquery.resolve_column_source_table_names(select, x_id)
            # frozenset({"dwd.a", "dwd.b"})
        """
        scope = self._resolve_select_scope(select)
        qualifier = str(column.table or "").replace('"', "").strip().lower()
        if qualifier:
            return scope.aliases.get(qualifier, frozenset())
        if len(scope.source_tables) == 1:
            return scope.source_tables
        return frozenset()

    def resolve_unique_column_source_table_name(
        self,
        select: exp.Select,
        column: exp.Column,
    ) -> str | None:
        """仅在 Column 唯一归属一张物理表时返回表名。

        该方法是维度和 JOIN 边抽取的便利接口。零张或多张候选表都返回
        ``None``，从而避免把歧义列关联到错误的数据集或业务域。

        下游任务：
            Dimension profile 解析 GROUP BY/WHERE 列；
            Biz-domain profile 解析 JOIN ON 两侧列。两者都要求精确的一对一物理表归属。

        完整一般示例::

            analysis = SqlAnalysis.create_from_sql(
                "SELECT o.amount FROM dwd.orders AS o WHERE o.status = 'paid'"
            )
            select = analysis.select_exprs[0]
            amount = next(select.expressions[0].find_all(exp.Column))
            analysis.resolve_unique_column_source_table_name(select, amount)
            # "dwd.orders"

        Corner cases::

            ambiguous = SqlAnalysis.create_from_sql(
                "SELECT id FROM dwd.orders o "
                "JOIN dim.users u ON o.user_id = u.id"
            )
            select = ambiguous.select_exprs[0]
            id_column = next(select.expressions[0].find_all(exp.Column))
            ambiguous.resolve_unique_column_source_table_name(select, id_column)
            # None  （未限定列，多表歧义）

            union_alias = SqlAnalysis.create_from_sql(
                "SELECT x.id FROM ("
                "SELECT id FROM dwd.a UNION ALL SELECT id FROM dwd.b"
                ") AS x"
            )
            select = union_alias.select_exprs[0]
            x_id = next(select.expressions[0].find_all(exp.Column))
            union_alias.resolve_unique_column_source_table_name(select, x_id)
            # None  （alias x 穿透到两张表）
        """
        tables = self.resolve_column_source_table_names(select, column)
        return next(iter(tables)) if len(tables) == 1 else None

    def resolve_expression_source_table_names(
        self,
        select: exp.Select,
        expression: exp.Expression,
    ) -> frozenset[str]:
        """返回表达式内所有 Column 依赖的物理表集合。

        如果表达式包含 Column，所有 Column 都必须解析；任一列存在歧义时
        返回空集合，避免产生不完整血缘。表达式完全不含 Column（例如
        ``COUNT(*)``）时，回退到当前 Select 的来源表。

        下游任务：
            Metric profile 解析复合指标和标量聚合的物理表依赖，并把结果交给
            ``convert_table_names_to_profile_table_ref`` 写入 metric ``table_name``。

        完整一般示例::

            analysis = SqlAnalysis.create_from_sql(
                "SELECT SUM(o.amount) / COUNT(u.id) AS ratio "
                "FROM dwd.orders AS o "
                "JOIN dim.users AS u ON o.user_id = u.id"
            )
            select = analysis.select_exprs[0]
            ratio_expression = select.expressions[0].this
            analysis.resolve_expression_source_table_names(select, ratio_expression)
            # frozenset({"dwd.orders", "dim.users"})

        Corner case：COUNT(*) 无 Column，回退到 Select 来源表::

            count_analysis = SqlAnalysis.create_from_sql(
                "SELECT COUNT(*) FROM dwd.orders o "
                "JOIN dim.users u ON o.user_id = u.id"
            )
            select = count_analysis.select_exprs[0]
            count_star = select.expressions[0]
            count_analysis.resolve_expression_source_table_names(select, count_star)
            # frozenset({"dwd.orders", "dim.users"})

        Corner case：存在无法归属的 Column，不返回部分血缘::

            ambiguous = SqlAnalysis.create_from_sql(
                "SELECT SUM(o.amount + id) "
                "FROM dwd.orders o "
                "JOIN dim.users u ON o.user_id = u.id"
            )
            select = ambiguous.select_exprs[0]
            metric_expression = select.expressions[0]
            ambiguous.resolve_expression_source_table_names(select, metric_expression)
            # frozenset()  （o.amount 可解析，但未限定 id 有歧义）

        Corner case：单表未限定列可以解析::

            single = SqlAnalysis.create_from_sql("SELECT SUM(amount) FROM dwd.orders")
            select = single.select_exprs[0]
            single.resolve_expression_source_table_names(select, select.expressions[0])
            # frozenset({"dwd.orders"})
        """
        columns = tuple(expression.find_all(exp.Column))
        if not columns and isinstance(expression, exp.Column):
            columns = (expression,)
        if not columns:
            return self.resolve_select_source_table_names(select)

        # 表达式血缘采用“全部可解析”语义，避免返回看似完整的部分结果。
        resolved: set[str] = set()
        for column in columns:
            column_tables = self.resolve_column_source_table_names(select, column)
            if not column_tables:
                return frozenset()
            resolved.update(column_tables)
        return frozenset(resolved)

    def convert_table_names_to_profile_table_ref(self, tables: Iterable[str]) -> str:
        """把物理表集合格式化为 profile DataFrame 的兼容字符串。

        输入会先规范化、去重和排序。空集合输出 ``_``，单表输出表名，
        多表使用逗号连接。该方法只在输出边界使用；
        内部血缘保留集合形式。

        下游任务：
            Metric profile 把表达式依赖集合转换成 DataFrame ``table_name``；
            ``_`` 只作为现有 CSV 兼容值，不应再参与内部血缘推断。

        完整一般示例::

            analysis = SqlAnalysis.create_from_sql("SELECT 1")
            analysis.convert_table_names_to_profile_table_ref([])
            # "_"
            analysis.convert_table_names_to_profile_table_ref({"dwd.orders"})
            # "dwd.orders"
            analysis.convert_table_names_to_profile_table_ref({"dim.users", "dwd.orders"})
            # "dim.users,dwd.orders"

        Corner cases::

            analysis.convert_table_names_to_profile_table_ref(
                [" DWD.Orders ", "dwd.orders", "dim.users"]
            )
            # "dim.users,dwd.orders"  （规范化、去重、排序）

            analysis.convert_table_names_to_profile_table_ref(["orders\nSELECT 1"])
            # "_"  （畸形名称被过滤）
        """
        normalized = sorted(
            {
                name
                for table in tables
                if (name := _transform_table_name_by_normalization(str(table)))
                is not None
            }
        )
        if not normalized:
            return "_"
        if len(normalized) == 1:
            return normalized[0]
        return ",".join(normalized)

    @staticmethod
    def extract_column_name(column: exp.Column) -> str:
        """返回移除双引号并转成小写的列名，不包含 table qualifier。

        下游任务：
            Dimension profile 生成 ``dimension_name``；Metric profile 生成
            ``agg_column``；Biz-domain profile 生成 ``on_columns`` 证据。

        完整示例::

            root = sqlglot.parse_one('SELECT O."User_ID" FROM dwd.orders AS O')
            column = next(root.find_all(exp.Column))
            SqlAnalysis.extract_column_name(column)
            # "user_id"

        Corner cases::

            SqlAnalysis.extract_column_name(exp.column("*"))
            # "*"

            SqlAnalysis.extract_column_name(exp.Column())
            # ""
        """
        return str(column.name or "").replace('"', "").lower()

    @staticmethod
    def extract_conjunct_exprs(
        expression: exp.Expression | None,
    ) -> tuple[exp.Expression, ...]:
        """把顶层 AND 链拆成保持原顺序的条件 tuple。

        OR 和括号子树保持整体，不再拆分；输入 ``None`` 返回空 tuple。

        下游任务：
            Dimension profile 拆分 WHERE 顶层 AND 条件；
            Biz-domain profile 拆分 JOIN ON 中的多个等值条件。

        完整一般示例::

            root = sqlglot.parse_one(
                "SELECT * FROM dwd.orders "
                "WHERE a = 1 AND b = 2 AND (c = 3 OR d = 4)"
            )
            condition = root.args["where"].this
            parts = SqlAnalysis.extract_conjunct_exprs(condition)
            tuple(part.sql() for part in parts)
            # ("a = 1", "b = 2", "(c = 3 OR d = 4)")

        Corner cases::

            SqlAnalysis.extract_conjunct_exprs(None)
            # ()

            root = sqlglot.parse_one(
                "SELECT * FROM dwd.orders WHERE a = 1 OR b = 2"
            )
            SqlAnalysis.extract_conjunct_exprs(root.args["where"].this)
            # (or_expression,)  （OR 不拆分）
        """
        if expression is None:
            return ()

        result: list[exp.Expression] = []
        stack: list[exp.Expression] = [expression]
        while stack:
            current = stack.pop()
            if isinstance(current, exp.And):
                stack.append(current.right)
                stack.append(current.left)
            else:
                result.append(current)
        return tuple(result)

    def _resolve_select_scope(self, select: exp.Select) -> _SelectScope:
        """读取当前实例中一个 Select 的预构建作用域。

        下游任务：
            不被 profile 直接调用；是 ``resolve_select_source_table_names`` 和列归属方法访问
            `_SelectScope` 的统一内部入口。

        完整一般示例::

            analysis = SqlAnalysis.create_from_sql("SELECT * FROM dwd.orders")
            scope = analysis._resolve_select_scope(analysis.select_exprs[0])
            scope.source_tables
            # frozenset({"dwd.orders"})

        Corner case::

            other = SqlAnalysis.create_from_sql("SELECT * FROM dim.users")
            analysis._resolve_select_scope(other.select_exprs[0])
            # raises ValueError("select does not belong to this SqlAnalysis")
        """
        scope = self._indexes.scopes_by_select_id.get(id(select))
        if scope is None:
            raise ValueError("select does not belong to this SqlAnalysis")
        return scope


# ==========================================
# 内部 SQL 清洗和表名规范化函数
# ==========================================
def _transform_sql_by_removing_unquoted_line_comments(text: str, marker: str) -> str:
    """删除指定 marker 开始的行注释，同时保留引号内相同字符。

    参数：
        text: 待处理 SQL 文本。
        marker: 行注释标记，当前用于 ``--`` 和 ``#``。

    下游任务：
        仅由 ``_transform_raw_sql_to_clean_sql`` 调用，间接服务于所有 profile 的 SQL 解析入口。

    完整一般示例::

        result = _transform_sql_by_removing_unquoted_line_comments(
            "SELECT '#1' AS code # remove me",
            "#",
        )
        result
        # "SELECT '#1' AS code "

    Corner cases::

        _transform_sql_by_removing_unquoted_line_comments(
            "SELECT '--not-comment' AS text -- remove",
            "--",
        )
        # "SELECT '--not-comment' AS text "

        _transform_sql_by_removing_unquoted_line_comments("SELECT 1", "")
        # "SELECT 1"  （空 marker 不修改输入）

        _transform_sql_by_removing_unquoted_line_comments("SELECT 'it''s #1' # x", "#")
        # "SELECT 'it''s #1' "  （SQL 双引号转义形式被保留）
    """
    if not marker:
        return text

    output: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        # 整段复制字符串字面量，并识别 SQL 中两个连续引号表示的转义引号。
        if char in ("'", '"'):
            quote = char
            output.append(char)
            index += 1
            while index < length:
                current = text[index]
                output.append(current)
                index += 1
                if current != quote:
                    continue
                if index < length and text[index] == quote:
                    output.append(text[index])
                    index += 1
                    continue
                break
            continue

        # 只有引号外命中的 marker 才是注释，换行符保留以维持 SQL 结构。
        if text.startswith(marker, index):
            while index < length and text[index] not in "\n\r":
                index += 1
            continue

        output.append(char)
        index += 1
    return "".join(output)


def _transform_raw_sql_to_clean_sql(sql: str) -> str:
    """清洗历史 query log 中的注释、控制字符和 Excel 换行标记。

    清洗不折叠普通空白，也不修改引号中的 ``#`` 或 ``--``。空输入返回空
    字符串，由 :meth:`SqlAnalysis.create_from_sql` 转换为 ``ValueError``。

    下游任务：
        ``SqlAnalysis.create_from_sql`` 的第一步；清洗结果被 Dataset profile 写入
        ``repeated_sql``，同时影响所有 profile 的 AST 解析。

    完整一般示例::

        result = _transform_raw_sql_to_clean_sql(
            "SELECT '#1' AS code_x000D_ /* block */\n"
            "FROM dwd.orders -- tail"
        )
        result
        # "SELECT '#1' AS code   \nFROM dwd.orders"

    Corner cases::

        _transform_raw_sql_to_clean_sql(" -- comment only")
        # ""

        _transform_raw_sql_to_clean_sql("SELECT 'a--b', 'c#d'")
        # "SELECT 'a--b', 'c#d'"

        _transform_raw_sql_to_clean_sql("SELECT\x00 1")
        # "SELECT  1"
    """
    cleaned = str(sql or "")
    # 先处理日志/Excel 引入的非 SQL 字符，再进行注释扫描。
    cleaned = cleaned.replace("_x000D_", " ").replace("\r", "\n")
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", cleaned)

    # 循环移除块注释，以兼容清除一段后暴露出下一段注释的历史脏数据。
    previous: str | None = None
    while previous != cleaned:
        previous = cleaned
        cleaned = re.sub(r"/\*[\s\S]*?\*/", " ", cleaned)

    cleaned = _transform_sql_by_removing_unquoted_line_comments(cleaned, "--")
    cleaned = _transform_sql_by_removing_unquoted_line_comments(cleaned, "#")
    return cleaned.strip()


def _transform_table_name_by_normalization(name: str) -> str | None:
    """规范化并校验候选物理表名。

    名称会被 strip 并转成小写。空名称、换行或包含明显 SQL
    关键片段的值返回 ``None``。

    下游任务：
        物理表识别、完整表名生成和 Metric profile 输出格式化的共同名称边界。

    完整一般示例::

        result = _transform_table_name_by_normalization(" DWD.Orders ")
        result
        # "dwd.orders"

    Corner cases::
        _transform_table_name_by_normalization("orders\nSELECT 1")
        # None

        _transform_table_name_by_normalization("")
        # None
    """
    normalized = str(name or "").strip().lower()
    if not normalized or "\n" in normalized or "\r" in normalized:
        return None
    if len(normalized) > 256:
        return None
    if re.search(
        r"\b(select|from|where|union|join|group\s+by|order\s+by)\b",
        normalized,
    ):
        return None
    return normalized


def _transform_table_name_to_reference(table: exp.Table) -> str:
    """把 Table AST 转换为规范化的完整小写名称。

    下游任务：
        ``_analyze_relation_expr`` 读取物理表/CTE 引用，间接服务于所有 profile。

    完整一般示例::

        root = sqlglot.parse_one("SELECT * FROM lake.sales.Orders")
        table = next(root.find_all(exp.Table))
        _transform_table_name_to_reference(table)
        # "lake.sales.orders"

    Corner cases::

        root = sqlglot.parse_one('SELECT * FROM "Sales"."Orders"')
        _transform_table_name_to_reference(next(root.find_all(exp.Table)))
        # "sales.orders"
    """
    parts: list[str] = []
    for node in (table.catalog, table.db, table.name):
        if node is None:
            fragment = ""
        elif isinstance(node, exp.Identifier):
            fragment = str(node.name or "")
        else:
            fragment = str(node)
        fragment = fragment.replace('"', "").strip().lower()
        if fragment:
            parts.append(fragment)
    return _transform_table_name_by_normalization(".".join(parts)) or ""


# ==========================================
# 关系别名提取函数
# ==========================================
def _extract_relation_alias(relation: exp.Expression) -> str | None:
    """返回 FROM/JOIN 关系的小写 alias。

    物理表无显式 alias 时回退到最后一段表名；无 alias 的子查询或其他关系
    返回 ``None``。

    下游任务：
        ``_build_select_scope`` 建立 alias map，最终服务于 Dimension、Metric 和
        Biz-domain 的 Column 表归属。

    完整一般示例::

        select = sqlglot.parse_one("SELECT * FROM dwd.orders AS o")
        relation = select.args["from_"].this
        _extract_relation_alias(relation)
        # "o"

        select = sqlglot.parse_one("SELECT * FROM dwd.orders")
        relation = select.args["from_"].this
        _extract_relation_alias(relation)
        # "orders"

    Corner cases::

        select = sqlglot.parse_one("SELECT * FROM (SELECT 1) AS X")
        _extract_relation_alias(select.args["from_"].this)
        # "x"

        select = sqlglot.parse_one("SELECT * FROM (SELECT 1)")
        _extract_relation_alias(select.args["from_"].this)
        # None
    """
    alias = relation.args.get("alias")
    if isinstance(alias, str):
        return alias.replace('"', "").lower()
    if isinstance(alias, exp.TableAlias):
        identifier = alias.this
        if isinstance(identifier, exp.Identifier):
            return str(identifier.name or "").replace('"', "").lower()
        return str(identifier or "").replace('"', "").lower()
    if isinstance(relation, exp.Table):
        return str(relation.name or "").replace('"', "").lower()
    return None


def _extract_select_source_exprs(select: exp.Select) -> tuple[exp.Expression, ...]:
    """按 SQL 书写顺序，提取 SELECT 语句中所有的数据源（Source）节点。

    在 SQL AST 中，数据源通常分布在 ``FROM`` 子句和各个 ``JOIN`` 子句中。
    本函数负责将它们统一提取出来，并按出现顺序返回。

    提取逻辑：
        1. 仅提取当前层级：优先从 ``FROM`` 子句中取出第一个直接数据源节点；
        2. 仅提取当前层级：遍历当前 ``SELECT`` 直接挂载的 ``JOIN`` 节点，依次取出其右侧数据源；
        3. 返回包含这些直接数据源节点的元组（若没有数据源则返回空元组）。
        注：本函数不做递归穿透，若数据源为子查询，返回的是子查询整体节点。

    参数：
        select: SQLGlot 的 ``Select`` 表达式节点。

    返回：
        按 SQL 顺序排列的数据源表达式元组。
        这些节点可能是 ``Table``、``Subquery`` 或 ``CTE`` 引用等。

    下游任务：
        作为 ``_analyze_query_expr`` 的统一数据源入口；解析后的结果再交给
        ``_build_select_scope``，间接服务于所有需要表血缘分析的 profile。

    示例::

        # 1. 提取 FROM 和 JOIN 中的多个数据源
        select = sqlglot.parse_one(
            "SELECT * FROM dwd.a AS a "
            "JOIN dwd.b AS b ON a.id = b.id "
            "JOIN dim.c AS c ON b.c_id = c.id"
        )
        sources = _extract_select_source_exprs(select)
        tuple(_extract_relation_alias(s) for s in sources)  # ("a", "b", "c")

        # 2. 子查询整体被视为一个数据源
        select = sqlglot.parse_one("SELECT * FROM (SELECT * FROM dwd.a) AS x")
        len(_extract_select_source_exprs(select))  # 1

        # 3. 无 FROM 子句时，安全返回空元组
        _extract_select_source_exprs(sqlglot.parse_one("SELECT 1"))  # ()
    """
    sources: list[exp.Expression] = []
    from_clause = select.args.get("from_") or select.args.get("from")
    if from_clause is not None and from_clause.this is not None:
        sources.append(from_clause.this)
    for join in select.args.get("joins") or ():
        if isinstance(join, exp.Join) and join.this is not None:
            sources.append(join.this)
    return tuple(sources)


def _unwrap_node_to_query_expr(
    node: exp.Expression | None,
) -> _QueryExpression | None:
    """剥离外层包装，提取最内层的真实查询节点。

    在 SQL AST 中，一个查询经常被 ``Subquery``、``Paren`` 或 ``CTE`` 等
    节点层层包裹（例如 ``((SELECT 1))``）。本函数像“剥洋葱”一样，
    沿着 ``.this`` 属性向下逐层穿透，直到找到真正的查询主体。

    解包逻辑：
        1. 若当前节点是 ``Select``/``Union``/``Intersect``/``Except``，直接返回；
        2. 若当前节点是 ``Subquery``/``Paren``/``CTE``，取 ``.this`` 继续向下；
        3. 遇到其他类型或超过最大深度 ``_MAX_UNWRAP_DEPTH``，返回 ``None``。

    参数：
        node: AST 节点。支持查询节点本身、包裹查询的包装节点、``None`` 或其他表达式。

    返回：
        解包后的查询节点（与原 AST 共享引用，不做深拷贝）。
        若无法解包为有效查询，返回 ``None``。

    下游任务：
        作为作用域索引和血缘分析的**统一入口**，屏蔽外层包装差异，
        让下游只需关心 ``Select``/``Union`` 等核心查询结构。

    示例::

        # 1. 剥离 Subquery
        sub = sqlglot.parse_one("SELECT * FROM (SELECT 1) AS x").args["from_"].this
        _unwrap_node_to_query_expr(sub).sql()  # "SELECT 1"

        # 2. 剥离多层 Paren
        paren = sqlglot.parse_one("(((SELECT 1)))")
        _unwrap_node_to_query_expr(paren).sql()  # "SELECT 1"

        # 3. 本身就是查询，直接返回（同一对象）
        union = sqlglot.parse_one("SELECT 1 UNION SELECT 2")
        _unwrap_node_to_query_expr(union) is union  # True

        # 4. 非查询节点，返回 None
        _unwrap_node_to_query_expr(exp.column("id"))  # None
        _unwrap_node_to_query_expr(None)              # None
    """
    current = node
    for _ in range(_MAX_UNWRAP_DEPTH):
        if current is None:
            return None
        if isinstance(current, _QUERY_EXPRESSIONS):
            return current
        if isinstance(current, (exp.Subquery, exp.Paren, exp.CTE)):
            current = current.this
            continue
        return None
    return None


def _extract_direct_child_query_exprs(
    query: _QueryExpression,
) -> tuple[_QueryExpression, ...]:
    """返回 query 中直接嵌套的子查询，不跨越另一个 query 或 CTE body。

    该函数建立词法作用域：父查询只把可见 CTE 传给直接子查询，嵌套
    更深的查询由子查询递归处理。

    下游任务：
        只由 ``_analyze_query_expr`` 使用，用于为 scalar subquery、EXISTS 和
        FROM subquery 建立各自的 Select scope。

    完整一般示例::

        root = sqlglot.parse_one(
            "SELECT * "
            "FROM (SELECT * FROM dwd.orders) AS o "
            "WHERE EXISTS (SELECT 1 FROM dim.users)"
        )
        children = _extract_direct_child_query_exprs(root)
        tuple(child.sql() for child in children)
        # (
        #   "SELECT * FROM dwd.orders",
        #   "SELECT 1 FROM dim.users",
        # )

    Corner cases：CTE body 由 WITH 声明顺序单独处理，不在结果中重复出现::

        root = sqlglot.parse_one(
            "WITH x AS (SELECT * FROM dwd.orders) SELECT * FROM x"
        )
        _extract_direct_child_query_exprs(root)
        # ()
    """
    output: list[_QueryExpression] = []
    stack: list[exp.Expression] = []
    # 从 query 的直接参数开始遍历，避免把 query 本身误判为子查询。
    for value in query.args.values():
        if isinstance(value, exp.Expression):
            stack.append(value)
        elif isinstance(value, (list, tuple)):
            stack.extend(item for item in value if isinstance(item, exp.Expression))

    while stack:
        current = stack.pop()
        if isinstance(current, exp.CTE):
            continue
        if isinstance(current, exp.Subquery):
            # Subquery 节点保存 alias；作用域递归只需要其内部的查询节点。
            child = _unwrap_node_to_query_expr(current.this)
            if child is not None:
                output.append(child)
            continue
        if isinstance(current, _QUERY_EXPRESSIONS) and current is not query:
            output.append(current)
            continue
        for value in current.args.values():
            if isinstance(value, exp.Expression):
                stack.append(value)
            elif isinstance(value, (list, tuple)):
                stack.extend(item for item in value if isinstance(item, exp.Expression))

    # 栈遍历会反转发现顺序；恢复 SQL 顺序后按 AST 身份去重。
    output.reverse()
    seen: set[int] = set()
    unique: list[_QueryExpression] = []
    for child in output:
        if id(child) in seen:
            continue
        seen.add(id(child))
        unique.append(child)
    return tuple(unique)


# ==========================================
# CTE 的处理
# ==========================================
def _extract_cte_name(cte: exp.CTE) -> str:
    """返回 CTE 的小写名称；没有合法 alias 时返回空字符串。

    下游任务：
        ``_analyze_query_expr`` 构建词法 CTE map，最终服务于所有 profile 的
        CTE 穿透。

    完整一般示例::

        root = sqlglot.parse_one(
            "WITH Paid AS (SELECT * FROM dwd.orders) SELECT * FROM Paid"
        )
        paid_cte = next(root.find_all(exp.CTE))
        _extract_cte_name(paid_cte)
        # "paid"

    Corner case：对于人工构造且无 alias 的 ``exp.CTE``:
        _extract_cte_name(exp.CTE(this=exp.select("1")))
        # ""
    """
    alias = cte.args.get("alias")
    if isinstance(alias, exp.TableAlias):
        identifier = alias.this
        if isinstance(identifier, exp.Identifier):
            return str(identifier.name or "").replace('"', "").lower()
        return str(identifier or "").replace('"', "").lower()
    alias_or_name = getattr(cte, "alias_or_name", None)
    return str(alias_or_name or "").replace('"', "").lower()


def _extract_declared_cte_exprs(query: _QueryExpression) -> tuple[exp.CTE, ...]:
    """返回直接声明在 query 上的 CTE，兼容 ``with``/``with_`` 参数名。

    下游任务：
        ``_analyze_query_expr`` 按声明顺序处理 CTE。

    完整一般示例::

        root = sqlglot.parse_one(
            "WITH a AS (SELECT * FROM dwd.a), "
            "b AS (SELECT * FROM dwd.b) SELECT * FROM a"
        )
        tuple(_extract_cte_name(cte) for cte in _extract_declared_cte_exprs(root))
        # ("a", "b")

    Corner case::

        _extract_declared_cte_exprs(sqlglot.parse_one("SELECT * FROM dwd.a"))
        # ()
    """
    with_clause = query.args.get("with_") or query.args.get("with")
    if not isinstance(with_clause, exp.With):
        return ()
    return tuple(cte for cte in with_clause.expressions if isinstance(cte, exp.CTE))


# Query 和 Relation 的递归严格遵循“只读上下文向下传递，分析增量向上返回”。
# 两个函数允许按 SQL 语法结构相互调用，但不会修改调用方持有的 Mapping 或集合。


def _analyze_query_expr(
    query_expr: _QueryExpression,
    visible_cte_table_names: Mapping[str, frozenset[str]],
    active_query_ids: frozenset[int],
) -> _AnalysisFragment:
    """递归分析一个查询，并显式返回本次新增的血缘和 Select 索引。

    参数：
        query_expr: Select、Union、Intersect 或 Except 查询表达式。
        visible_cte_table_names: 当前词法位置可见的 CTE 名到物理表集合映射。
        active_query_ids: 当前递归路径上的 Query id；使用不可变集合避免修改
            调用方状态。

    返回：
        ``_AnalysisFragment``。``source_table_names`` 只描述当前 Query 的
        FROM/JOIN 或集合分支血缘；两个索引 Mapping 包含整个 Query 子树中新
        分析出的 Select。

    下游任务：
        ``_build_analysis_indexes`` 使用它一次完成 CTE lineage、子查询遍历和
        Select scope 构建；``_analyze_relation_expr`` 在关系包含查询时回调它。

    完整一般示例::

        query = sqlglot.parse_one(
            "WITH paid AS (SELECT * FROM dwd.orders) SELECT * FROM paid"
        )
        result = _analyze_query_expr(query, {}, frozenset())
        result.source_table_names
        # frozenset({"dwd.orders"})
        len(result.scopes_by_select_id)
        # 2

    Corner cases::

        query = sqlglot.parse_one("SELECT 1")
        _analyze_query_expr(query, {}, frozenset()).source_table_names
        # frozenset()

        blocked = _analyze_query_expr(query, {}, frozenset({id(query)}))
        blocked.source_table_names
        # frozenset()  （递归保护命中，且两个索引 Mapping 也为空）

        query = sqlglot.parse_one(
            "SELECT * FROM dwd.a UNION ALL SELECT * FROM dwd.b"
        )
        _analyze_query_expr(query, {}, frozenset()).source_table_names
        # frozenset({"dwd.a", "dwd.b"})
    """
    unwrapped = _unwrap_node_to_query_expr(query_expr)
    if unwrapped is None or id(unwrapped) in active_query_ids:
        return _EMPTY_ANALYSIS_FRAGMENT

    next_active_query_ids = active_query_ids | frozenset({id(unwrapped)})
    visible_ctes_by_select_id: dict[int, Mapping[str, frozenset[str]]] = {}
    scopes_by_select_id: dict[int, _SelectScope] = {}

    # CTE 按声明顺序扩展。每个 body 的完整分析结果只计算一次并向上合并。
    current_visible_ctes = dict(visible_cte_table_names)
    for cte in _extract_declared_cte_exprs(unwrapped):
        cte_name = _extract_cte_name(cte)
        cte_body = _unwrap_node_to_query_expr(cte.this)
        if not cte_name or cte_body is None:
            continue
        cte_result = _analyze_query_expr(
            cte_body,
            current_visible_ctes,
            next_active_query_ids,
        )
        visible_ctes_by_select_id.update(cte_result.visible_ctes_by_select_id)
        scopes_by_select_id.update(cte_result.scopes_by_select_id)
        current_visible_ctes[cte_name] = cte_result.source_table_names

    source_table_names: set[str] = set()
    if isinstance(unwrapped, exp.Select):
        resolved_sources: list[_ResolvedSelectSource] = []
        source_query_ids: set[int] = set()
        for source_expr in _extract_select_source_exprs(unwrapped):
            source_query = _unwrap_node_to_query_expr(source_expr)
            if source_query is not None:
                source_query_ids.add(id(source_query))

            relation_result = _analyze_relation_expr(
                source_expr,
                current_visible_ctes,
                next_active_query_ids,
            )
            resolved_sources.append((source_expr, relation_result.source_table_names))
            visible_ctes_by_select_id.update(relation_result.visible_ctes_by_select_id)
            scopes_by_select_id.update(relation_result.scopes_by_select_id)

        select_scope = _build_select_scope(tuple(resolved_sources))
        frozen_visible_ctes = MappingProxyType(
            {
                name: frozenset(table_names)
                for name, table_names in current_visible_ctes.items()
            }
        )
        visible_ctes_by_select_id[id(unwrapped)] = frozen_visible_ctes
        scopes_by_select_id[id(unwrapped)] = select_scope
        source_table_names.update(select_scope.source_tables)

        # FROM/JOIN 子查询已通过 relation 分析；这里只补充 scalar/EXISTS 子查询。
        for child_query in _extract_direct_child_query_exprs(unwrapped):
            if id(child_query) in source_query_ids:
                continue
            child_result = _analyze_query_expr(
                child_query,
                current_visible_ctes,
                next_active_query_ids,
            )
            visible_ctes_by_select_id.update(child_result.visible_ctes_by_select_id)
            scopes_by_select_id.update(child_result.scopes_by_select_id)

    elif isinstance(unwrapped, _SET_OPERATIONS):
        # 集合查询没有自己的 Select scope，只合并左右分支的显式返回结果。
        for branch_expr in (unwrapped.this, unwrapped.expression):
            branch_query = _unwrap_node_to_query_expr(branch_expr)
            if branch_query is None:
                continue
            branch_result = _analyze_query_expr(
                branch_query,
                current_visible_ctes,
                next_active_query_ids,
            )
            source_table_names.update(branch_result.source_table_names)
            visible_ctes_by_select_id.update(branch_result.visible_ctes_by_select_id)
            scopes_by_select_id.update(branch_result.scopes_by_select_id)

    return _AnalysisFragment(
        source_table_names=frozenset(source_table_names),
        visible_ctes_by_select_id=MappingProxyType(visible_ctes_by_select_id),
        scopes_by_select_id=MappingProxyType(scopes_by_select_id),
    )


def _analyze_relation_expr(
    relation_expr: exp.Expression,
    visible_cte_table_names: Mapping[str, frozenset[str]],
    active_query_ids: frozenset[int],
) -> _AnalysisFragment:
    """分析单个 FROM/JOIN 数据源并显式返回表血缘和嵌套 Select 索引。

    Table 优先按当前可见 CTE 解析，否则作为物理表返回；Subquery 和集合查询
    委托 ``_analyze_query_expr``。Values、UNNEST 等未支持关系返回空结果。

    下游任务：
        只由 ``_analyze_query_expr`` 调用。它让 Query 与 Relation 的自然相互
        递归保持“只读参数向下、``_AnalysisFragment`` 向上”的统一数据流。

    完整一般示例::

        root = sqlglot.parse_one("SELECT * FROM dwd.orders")
        relation = root.args["from_"].this
        result = _analyze_relation_expr(relation, {}, frozenset())
        result.source_table_names
        # frozenset({"dwd.orders"})
        result.scopes_by_select_id
        # {}  （普通 Table 不产生嵌套 Select）

    Corner cases::

        root = sqlglot.parse_one("SELECT * FROM paid")
        relation = root.args["from_"].this
        _analyze_relation_expr(
            relation,
            {"paid": frozenset({"dwd.orders"})},
            frozenset(),
        ).source_table_names
        # frozenset({"dwd.orders"})

        root = sqlglot.parse_one(
            "SELECT * FROM (SELECT * FROM dim.users) AS u"
        )
        relation = root.args["from_"].this
        result = _analyze_relation_expr(relation, {}, frozenset())
        result.source_table_names
        # frozenset({"dim.users"})
        len(result.scopes_by_select_id)
        # 1
    """
    current: exp.Expression | None = relation_expr
    for _ in range(_MAX_UNWRAP_DEPTH):
        if not isinstance(current, exp.Paren):
            break
        current = current.this
    if current is None:
        return _EMPTY_ANALYSIS_FRAGMENT

    # Table 名优先匹配当前词法作用域中的 CTE；其余合法表名都是物理表。
    if isinstance(current, exp.Table):
        table_name = _transform_table_name_to_reference(current)
        source_table_names = (
            visible_cte_table_names[table_name]
            if table_name in visible_cte_table_names
            else frozenset({table_name})
            if table_name
            else frozenset()
        )
        return _AnalysisFragment(
            source_table_names=frozenset(source_table_names),
            visible_ctes_by_select_id=MappingProxyType({}),
            scopes_by_select_id=MappingProxyType({}),
        )

    # 子查询 relation 不使用其 alias 作为表名，而是递归穿透内部查询。
    if isinstance(current, exp.Subquery):
        query = _unwrap_node_to_query_expr(current.this)
        if query is None:
            return _EMPTY_ANALYSIS_FRAGMENT
        return _analyze_query_expr(
            query,
            visible_cte_table_names,
            active_query_ids,
        )

    if isinstance(current, _QUERY_EXPRESSIONS):
        return _analyze_query_expr(
            current,
            visible_cte_table_names,
            active_query_ids,
        )
    return _EMPTY_ANALYSIS_FRAGMENT


# `_build_select_scope` 只消费已解析的数据源，不再解析 CTE、子查询或触发递归。
def _build_select_scope(
    resolved_sources: tuple[_ResolvedSelectSource, ...],
) -> _SelectScope:
    """根据已解析的数据源构建只读 alias map 和来源表集合。

    参数：
        resolved_sources: 按 SQL 顺序排列的 ``(source_expr, table_names)``。
            ``table_names`` 必须由 ``_analyze_relation_expr`` 预先计算。

    返回：
        不可变的 ``_SelectScope``。本函数不读取 CTE 上下文、不遍历子查询，也
        不调用任何递归函数。

    下游任务：
        ``_analyze_query_expr`` 完成 Relation 分析后调用本函数，为当前 Select
        建立缓存；最终被来源表和 Column resolution 公开方法消费。

    完整一般示例::

        select = sqlglot.parse_one(
            "SELECT o.id, u.name FROM dwd.orders AS o "
            "JOIN dim.users AS u ON o.user_id = u.id"
        )
        scope = _build_select_scope(
            (
                (select.args["from_"].this, frozenset({"dwd.orders"})),
                (select.args["joins"][0].this, frozenset({"dim.users"})),
            )
        )
        dict(scope.aliases)
        # {
        #   "o": frozenset({"dwd.orders"}),
        #   "u": frozenset({"dim.users"}),
        # }
        scope.source_tables
        # frozenset({"dwd.orders", "dim.users"})

    Corner case：UNION 子查询 alias 可映射多表::

        select = sqlglot.parse_one(
            "SELECT x.id FROM ("
            "SELECT id FROM dwd.a UNION ALL SELECT id FROM dwd.b"
            ") AS x"
        )
        scope = _build_select_scope(
            ((
                select.args["from_"].this,
                frozenset({"dwd.a", "dwd.b"}),
            ),)
        )
        scope.aliases["x"]
        # frozenset({"dwd.a", "dwd.b"})

        _build_select_scope(())
        # _SelectScope(aliases={}, source_tables=frozenset())
    """
    aliases: dict[str, set[str]] = {}
    source_tables: set[str] = set()
    for source_expr, source_table_names in resolved_sources:
        source_tables.update(source_table_names)
        alias = _extract_relation_alias(source_expr)
        if alias:
            aliases.setdefault(alias, set()).update(source_table_names)

    frozen_aliases = MappingProxyType(
        {alias: frozenset(tables) for alias, tables in aliases.items()}
    )
    return _SelectScope(
        aliases=frozen_aliases,
        source_tables=frozenset(source_tables),
    )


# ==========================================
# _build_analysis_indexes(root)
# └── _analyze_query_expr(
#         root_query,
#         visible_cte_table_names={},
#         active_query_ids=frozenset(),
#     )
#     ├── CTE/集合分支/子查询 -> 递归并返回 _AnalysisFragment
#     ├── Relation -> _analyze_relation_expr -> 必要时回调 Query 分析
#     └── Select -> _build_select_scope(resolved_sources)，不再触发递归
# ==========================================
def _build_analysis_indexes(root: exp.Expression) -> _AnalysisIndexes:
    """一次构建整条 SQL 的 Select、Join、CTE 和来源表索引。

    参数：
        root: SQLGlot AST 根节点。

    返回：
        由单次递归分析结果转换成的不可变 ``_AnalysisIndexes``。

    下游任务：
        ``SqlAnalysis.create_from_sql`` 创建上下文时调用一次；产物被 Dataset、Dimension、
        Metric 和 Biz-domain 的全部后续查询复用。

    完整一般示例::

        root = sqlglot.parse_one(
            "WITH paid AS (SELECT * FROM dwd.orders) "
            "SELECT * FROM paid p "
            "JOIN dim.users u ON p.user_id = u.id"
        )
        indexes = _build_analysis_indexes(root)
        indexes.physical_tables
        # frozenset({"dwd.orders", "dim.users"})
        tuple(select.sql() for select in indexes.selects)
        # (
        #   "WITH paid AS (SELECT * FROM dwd.orders) SELECT * FROM paid AS p "
        #   "JOIN dim.users AS u ON p.user_id = u.id",
        #   "SELECT * FROM dwd.orders",
        # )
        tuple(join.sql() for join in indexes.joins)
        # ("JOIN dim.users AS u ON p.user_id = u.id",)

    Corner cases::

        root = sqlglot.parse_one("SELECT 1")
        indexes = _build_analysis_indexes(root)
        indexes.physical_tables
        # frozenset()
        indexes.joins
        # ()

        root = sqlglot.parse_one(
            "WITH x AS (SELECT * FROM dwd.a UNION ALL SELECT * FROM dwd.b) "
            "SELECT * FROM x"
        )
        _build_analysis_indexes(root).physical_tables
        # frozenset({"dwd.a", "dwd.b"})
    """
    root_query = _unwrap_node_to_query_expr(root)
    if root_query is None:
        root_result = _EMPTY_ANALYSIS_FRAGMENT
    else:
        # 递归只接收只读上下文，所有新增索引都通过 fragment 显式返回。
        root_result = _analyze_query_expr(
            root_query,
            {},
            frozenset(),
        )

    visible_ctes_by_select_id = dict(root_result.visible_ctes_by_select_id)
    scopes_by_select_id = dict(root_result.scopes_by_select_id)

    selects = tuple(root.find_all(exp.Select))
    # 人工构造的异常 AST 可能不在标准 Query 路径中；兜底分析仍通过返回值合并。
    for select in selects:
        if id(select) in scopes_by_select_id:
            continue
        fallback_result = _analyze_query_expr(
            select,
            {},
            frozenset(),
        )
        visible_ctes_by_select_id.update(fallback_result.visible_ctes_by_select_id)
        scopes_by_select_id.update(fallback_result.scopes_by_select_id)

    # 整条 SQL 的物理表集合直接由各 Select scope 汇总，不再重复遍历 AST。
    physical_tables: set[str] = set()
    for scope in scopes_by_select_id.values():
        physical_tables.update(scope.source_tables)

    return _AnalysisIndexes(
        selects=selects,
        joins=tuple(root.find_all(exp.Join)),
        physical_tables=frozenset(physical_tables),
        visible_ctes_by_select_id=MappingProxyType(dict(visible_ctes_by_select_id)),
        scopes_by_select_id=MappingProxyType(dict(scopes_by_select_id)),
    )
