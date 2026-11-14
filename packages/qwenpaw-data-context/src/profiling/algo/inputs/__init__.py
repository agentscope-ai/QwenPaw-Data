from .probe_facts_input import ProbeFactsInput, StaticProbeFactsInput
from .query_log_base import QueryLogInput
from .table_input_base import TableInput

from . import (  # noqa: E402, F401 —— import 本包即注册全部方言（dialect_keys 自注册）
    query_log_postgres,
    query_log_sql_files,
    table_input_bigquery,
    table_input_postgres,
    table_input_sqlite,
)

# 结构守卫：凡 SQL 系方言认领的键，必有同键自建执行器 ——「pg 丢键」类
# 回归在 import 期即炸，不等运行期。按定义不执行 SQL 的日志来源
# （``executes_sql = False``，如文件来源）不在此列；将来若出现「仅经注入
# 可用」的方言（只走项目执行器），在此豁免即可。
from ..sql import SqlExecutor as _SqlExecutor
from .table_input_sql import SqlTableInput as _SqlTableInput

_unbacked = {
    key
    for impl in (*_SqlTableInput.__subclasses__(), *QueryLogInput._registry.values())
    if getattr(impl, "executes_sql", True)
    for key in impl.dialect_keys
    if key not in _SqlExecutor.supported()
}
if _unbacked:
    raise ImportError(f"方言键缺自建执行器: {sorted(_unbacked)}")
del _SqlExecutor, _SqlTableInput, _unbacked

__all__ = [
    "ProbeFactsInput",
    "QueryLogInput",
    "StaticProbeFactsInput",
    "TableInput",
]
