"""mg-088 自动探查：sample_values 注入形态防护（§3.6 第二层，裁决 Q4 批准加严）。

规则与 ``graph/embeddings.py`` sanitizer **同规则独立实现**（embed 簇并行实施
可能搬迁函数，避免跨卡 import 耦合；对齐测试见
tests/test_mg088_autoexplore_pipeline.py::test_auto_sanitizer_aligns_with_embeddings_rules）：

- 注入形态判定：``'`` / ``%20`` / ``\\b1\\s*=\\s*1\\b``（embeddings.py:75-86 同形）；
- 截长 32（SAMPLE_VALUE_MAX_LEN，embeddings.py:70 同值）；保序去重。

刻意偏离点（纪律登记，R6）：自动路径对注入形态值**不入图**（既有纪律「图里
原值不动」保护的是人工精修值；自动探查无人工审核环节，加严批准 = Q4）。
「全部」token 去除是语料面规则（非注入防护），本卡不做。
"""

from __future__ import annotations

import re

#: 单个样例值的最大长度（超长截断；与 embeddings.SAMPLE_VALUE_MAX_LEN 同值）
SAMPLE_VALUE_MAX_LEN = 32

#: 注入形态判定：布尔注入 `1=1`（容忍空格/大小写）—— embeddings._INJECTION_RE 同形
_INJECTION_RE = re.compile(r"\b1\s*=\s*1\b", re.IGNORECASE)


def is_injection_shaped(value: str) -> bool:
    """含单引号 / URL 编码空格 ``%20`` / 布尔注入形态 → True。"""
    if "'" in value:
        return True
    if "%20" in value.lower():
        return True
    return bool(_INJECTION_RE.search(value))


def sanitize_auto_samples(values) -> list[str]:
    """自动探查采样值净化（序）：去空白空值 → 去注入形态值 → 截长 → 保序去重。"""
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        values = [values]
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        v = str(raw).strip() if raw is not None else ""
        if not v:
            continue
        if is_injection_shaped(v):
            continue
        v = v[:SAMPLE_VALUE_MAX_LEN]
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out
