"""semantic_config DB 依赖作用域回归测试。

``get_db`` 的 commit 在依赖 teardown 里执行。FastAPI 默认（request 作用域）
在响应发出**之后**才运行 teardown，客户端拿到 200 后立刻发起的下一个请求会
用新连接读不到未提交的写入（CI 上 metric create→update 偶发 404 的根因）。
所有 ``Depends(get_db)`` 必须显式 ``scope="function"``，让 commit 先于响应。
"""
from __future__ import annotations

import ast
from pathlib import Path

ROUTERS_DIR = (
    Path(__file__).resolve().parents[1] / "src" / "semantic_config" / "routers"
)


def _iter_get_db_depends() -> list[tuple[Path, int, str | None]]:
    found: list[tuple[Path, int, str | None]] = []
    for path in sorted(ROUTERS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Name) and node.func.id == "Depends"):
                continue
            if not (
                node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "get_db"
            ):
                continue
            scope = None
            for keyword in node.keywords:
                if keyword.arg == "scope" and isinstance(
                    keyword.value, ast.Constant
                ):
                    scope = keyword.value.value
            found.append((path, node.lineno, scope))
    return found


def test_get_db_dependencies_are_discovered() -> None:
    assert _iter_get_db_depends(), "expected Depends(get_db) usages in routers"


def test_get_db_dependencies_use_function_scope() -> None:
    offenders = [
        f"{path.name}:{line} scope={scope!r}"
        for path, line, scope in _iter_get_db_depends()
        if scope != "function"
    ]
    assert not offenders, (
        "Depends(get_db) must pass scope=\"function\" so the per-request "
        "commit runs before the response is sent (read-after-write race "
        "otherwise): " + "; ".join(offenders)
    )
