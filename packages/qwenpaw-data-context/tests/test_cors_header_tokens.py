"""CORS 头字段合法性回归测试。

背景：品牌重命名曾把 allow_headers 里的 ``X-Datapaw-Run`` 误替换成含空格的
``X-QwenPaw Data-Run``。空格不是合法的 header token 字符，浏览器会因此拒绝
解析整个 ``Access-Control-Allow-Headers``，导致所有需要预检的跨源请求
（如前端的 JSON POST）全部失败。本测试静态扫描包内所有 CORS 头配置，
确保每个 token 合法。
"""
from __future__ import annotations

import ast
import string
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src"

# RFC 7230 token 字符集（header 名称合法字符）。
_TOKEN_CHARS = set("!#$%&'*+-.^_`|~" + string.ascii_letters + string.digits)

_HEADER_LIST_KWARGS = {"allow_headers", "expose_headers"}


def _is_valid_header_token(value: str) -> bool:
    return bool(value) and all(ch in _TOKEN_CHARS for ch in value)


def _iter_header_tokens() -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg not in _HEADER_LIST_KWARGS:
                    continue
                if not isinstance(keyword.value, (ast.List, ast.Tuple)):
                    continue
                for element in keyword.value.elts:
                    if isinstance(element, ast.Constant) and isinstance(
                        element.value, str
                    ):
                        found.append((path, element.value))
    return found


def test_cors_header_configs_are_discovered() -> None:
    tokens = _iter_header_tokens()
    assert tokens, "expected at least one allow_headers/expose_headers config"


def test_cors_header_tokens_are_valid() -> None:
    invalid = [
        f"{path.relative_to(PACKAGE_ROOT)}: {value!r}"
        for path, value in _iter_header_tokens()
        if not _is_valid_header_token(value)
    ]
    assert not invalid, (
        "CORS header lists contain invalid header tokens (browsers reject the "
        "entire preflight response over these): " + "; ".join(invalid)
    )
