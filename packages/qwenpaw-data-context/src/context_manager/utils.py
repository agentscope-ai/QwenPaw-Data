"""通用小工具：日志初始化与 LLM 输出 SQL 清洗。"""
from __future__ import annotations

import logging  # 标准库日志
import re  # 正则：剥离 markdown 代码块等
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Optional

# 模块级根 logger 名，子 logger 可继承其 handler
_logger = logging.getLogger("context_manager")


def get_logger(name: str = "context_manager") -> logging.Logger:
    """返回带控制台 handler 的 logger；首次调用时配置格式与级别。"""
    if not _logger.handlers:  # 尚未配置过根 context_manager logger
        h = logging.StreamHandler()  # 输出到 stderr
        h.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))
        _logger.addHandler(h)
        _logger.setLevel(logging.INFO)  # 默认 INFO
        _logger.propagate = False  # 不向 root logger 冒泡，避免重复打印
    logger = logging.getLogger(name)  # 子 logger（如 ingest、pipeline）
    if not logger.handlers:  # 子 logger 复用同一套 handler
        for h in _logger.handlers:
            logger.addHandler(h)
        logger.setLevel(_logger.level)
        logger.propagate = False
    return logger


# 社区版只有默认 neo4j 库
ALLOWED_NEO4J_LOGICAL_DBS = frozenset({"neo4j"})

# HTTP 中间件或线程池任务注入当前请求的 Neo4j 逻辑库（供 neo4j_session 读取）
neo4j_database_ctx: ContextVar[Optional[str]] = ContextVar("neo4j_database_ctx", default=None)


def pick_neo4j_database(header_value: Optional[str]) -> Optional[str]:
    """根据客户端 ``X-Neo4j-Database`` 解析逻辑库名。

    非空 header 必须是 :data:`ALLOWED_NEO4J_LOGICAL_DBS` 之一；否则抛 ``ValueError``。
    空 / 缺省则使用 ``CFG.neo4j_database``（可能为 ``None``，即服务器默认库）。
    """
    raw = (header_value or "").strip()
    if raw:
        if raw not in ALLOWED_NEO4J_LOGICAL_DBS:
            raise ValueError(f"Unknown Neo4j logical database: {raw!r}")
        return raw
    from .config import CFG

    return CFG.neo4j_database


@contextmanager
def neo4j_session(driver: Any, *, database: Optional[str] = None) -> Iterator[Any]:
    """打开 Neo4j session。

    优先级：显式 ``database`` 参数 > 请求上下文（Topology UI 的 ``X-Neo4j-Database``）>
    ``CFG.neo4j_database``。
    """
    from .config import CFG

    db = database
    if db is None:
        db = neo4j_database_ctx.get()
    if db is None:
        db = CFG.neo4j_database
    kwargs: dict = {}
    if db:
        kwargs["database"] = db
    sess = driver.session(**kwargs)
    try:
        yield sess
    finally:
        sess.close()


# 语义层导入等模块沿用 graph_session 旧名；与 neo4j_session 行为完全一致。
graph_session = neo4j_session


def strip_sql(sql: str) -> str:
    """去掉 LLM 常见的 ```sql ... ``` 围栏、前缀标签，并保证以分号结尾。"""
    sql = (sql or "").strip()  # 空则变 ""
    if not sql:
        return ""
    m = re.search(r"```(?:sql)?\s*(.*?)```", sql, flags=re.S | re.I)  # 非贪婪匹配 fenced block
    if m:
        sql = m.group(1).strip()  # 只取围栏内正文
    sql = re.sub(r"^\s*(?:sql|answer|final\s*sql)\s*[:：]\s*", "", sql, flags=re.I)  # 去掉 "SQL:" 类前缀
    return sql.strip().rstrip(";") + ";"  # 统一以单个分号结束，便于下游执行


# Markdown 小节标题：Explanation / SQL，以及中文「说明」「解释」
_SECTION_HEADER = re.compile(
    r"(?m)^\s{0,3}(#{1,4})\s*(Explanation|SQL|说明|解释)\s*$",
    re.I,
)


def parse_sql_and_explanation(raw: str) -> tuple[str, str]:
    """从模型回复中拆出 SQL 与简短说明。

    期望结构为 ``### Explanation`` / ``### SQL``（顺序任意）。若缺少小节标题，
    则把整个正文交给 :func:`strip_sql` 当作纯 SQL（兼容旧提示词）。
    """
    text = (raw or "").strip()
    if not text:
        return "", ""

    matches = list(_SECTION_HEADER.finditer(text))
    if not matches:
        return strip_sql(text), ""

    by_label: dict[str, str] = {}
    for i, m in enumerate(matches):
        lab = m.group(2).lower()
        if lab in ("说明", "解释", "explanation"):
            key = "explanation"
        elif lab == "sql":
            key = "sql"
        else:
            continue
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        by_label[key] = text[start:end].strip()

    sql_raw = by_label.get("sql", "")
    expl = by_label.get("explanation", "")
    if sql_raw:
        return strip_sql(sql_raw), expl
    return strip_sql(text), ""
