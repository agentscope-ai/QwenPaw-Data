"""全局凭证脱敏。

挂在 root logger 上扫每条 LogRecord 的 msg / args / exc_info。三类 pattern：
1. key=value 形式的 password / token / secret / api_key（含 ``access_key_secret``）
2. AK 形式串：``AKIA...``（AWS） / ``LTAI...``（Aliyun）
3. ``-----BEGIN ... PRIVATE KEY-----`` block 与 ``"private_key": "..."``（GCP SA JSON）
"""
from __future__ import annotations

import logging
import re
import traceback
from typing import Iterable

# (1) key=value / key: value 形式
#
# 实施注记：``private_key_passphrase`` 不能靠 ``passphrase`` 子串 + ``\b`` 兜住,因为
# ``_`` 是 ``\w`` 字符,``private_key_passphrase`` 中 ``_passphrase`` 之间没有 ``\b``。
# 所以把 ``private_key_passphrase`` 显式列在 alternation 里(放在 ``private_key`` 之前
# 以匹配更长的前缀)。
_KV_PATTERN = re.compile(
    r"(?i)['\"]?\b(password|pwd|passwd|private_key_passphrase|passphrase|client_secret|access_key_secret|access_key_id|access_key|api_key|refresh_token|sts_token|token|auth|private_key|pem)['\"]?"
    r"\s*[:=]>?\s*['\"]?([^'\"\s,&}]+)",
)

# (2) cloud AK 显式形态
_AK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{12,20}"),              # AWS root + session
    re.compile(r"LTAI[A-Za-z0-9]{12,30}"),                    # Aliyun
)

# (3) PEM 私钥与 GCP SA JSON 内 private_key
_PEM_PATTERN = re.compile(
    r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC )?PRIVATE KEY-----",
)
_SA_PRIVATE_KEY_PATTERN = re.compile(r'"private_key"\s*:\s*"[^"]+"')

# (4) Authorization: Bearer ... 头
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]+")


def _redact_str(s: str) -> str:
    """对一段字符串做凭证 mask;幂等。

    顺序约定:先 PEM block / SA-JSON ``"private_key"``(长 / 多行 / 跨字段的整段),
    再 KV(单行 key=value),最后 AK / Bearer。否则 KV 模式可能以 ``private_key`` 关键字
    把 PEM 头吃成一个 KV 串,破坏 PEM 整段匹配。
    """
    if not s:
        return s
    out = _PEM_PATTERN.sub("[REDACTED]", s)
    out = _SA_PRIVATE_KEY_PATTERN.sub('"private_key": "[REDACTED]"', out)
    out = _KV_PATTERN.sub(lambda m: f"{m.group(1)}=[REDACTED]", out)
    for pat in _AK_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    out = _BEARER_PATTERN.sub("Bearer [REDACTED]", out)
    return out


def _redact_iter(values: Iterable[object]) -> tuple[object, ...]:
    """args / extra 里的 str 走 redact,其它原样。"""
    out: list[object] = []
    for v in values:
        if isinstance(v, str):
            out.append(_redact_str(v))
        else:
            out.append(v)
    return tuple(out)


class CredentialRedactFilter(logging.Filter):
    """挂在每个 handler 上(不要挂在 logger 上)。

    Python ``logging`` 语义：filter 装在 Logger 上仅作用于该 logger 直接发出的
    record；子 logger propagate 上来的 record 不会被 logger-level filter 处理。
    所以 Task 12 在 ``server.py`` 中遍历 ``logging.getLogger().handlers`` +
    uvicorn / fastapi handlers 全量 ``addFilter(CredentialRedactFilter())``。

    工作流：
    1. 计算最终 message(``record.getMessage()``)并 redact
    2. 清空 ``record.args`` 防止 handler 二次格式化时把原始明文塞回去
    3. ``record.exc_info`` 渲染为 redact 后的字符串,写入 ``record.exc_text``,
       并清掉 ``exc_info`` 避免 handler 重新调 ``format_exception``
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            record.msg = _redact_str(msg)
            record.args = ()
            if record.exc_info:
                exc_text = "".join(traceback.format_exception(*record.exc_info))
                record.exc_text = _redact_str(exc_text)
                record.exc_info = None
        except Exception:
            # 兜底:redact 自身出错不该把 log 丢掉
            pass
        return True
