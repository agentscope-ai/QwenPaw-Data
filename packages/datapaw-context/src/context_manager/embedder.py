"""统一向量编码入口；走 OpenAI 兼容 embeddings API（DashScope / SiliconFlow / 等）。

通过 ``CFG.embed_model`` 指定模型名，``CFG.embed_dim`` 指定向量维度。
"""
from __future__ import annotations

import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from typing import List, Optional

from .config import CFG

log = logging.getLogger(__name__)

# DashScope text-embedding-v3 单次最多 10 条 input（实测）；可被 EMBED_API_BATCH 覆盖
import os as _os
_OPENAI_BATCH_HARD_LIMIT = max(1, int(_os.getenv("EMBED_API_BATCH", "10")))
# DashScope compatible-mode burst rate ≈ 30 in-flight; keep slight headroom: default 24.
_OPENAI_CONCURRENCY = max(1, int(_os.getenv("EMBED_API_CONCURRENCY", "24")))
# 429 retry budget: each retry doubles sleep, base 0.5s → 0.5/1/2/4s = 7.5s max wait.
_OPENAI_MAX_RETRIES = max(0, int(_os.getenv("EMBED_API_MAX_RETRIES", "5")))
# DashScope text-embedding-v3 caps input length at 8192 (rejects whole batch with
# HTTP 400 "Range of input length should be [1, 8192]"). Large columns with
# multi-KB embedded JSON schema docs in description → trips this. Defensive
# clip to 8000 leaves headroom and never destroys source-of-truth text (only the
# string sent to the API; Neo4j Column.text / Column.description keep the full value).
_OPENAI_MAX_INPUT_CHARS = max(100, int(_os.getenv("EMBED_API_MAX_INPUT_CHARS", "8000")))


def _call_one_batch(client, model_name: str, chunk: List[str]) -> List[List[float]]:
    """Call embeddings.create with 429 exponential backoff. Returns vectors in input order."""
    for attempt in range(_OPENAI_MAX_RETRIES + 1):
        try:
            resp = client.embeddings.create(model=model_name, input=chunk)
            return [item.embedding for item in resp.data]
        except Exception as exc:
            msg = str(exc)
            # DashScope returns RateLimitError / "limit_burst_rate" / HTTP 429
            is_rate = "429" in msg or "rate" in msg.lower() or "limit_burst_rate" in msg
            if not is_rate or attempt == _OPENAI_MAX_RETRIES:
                raise
            sleep = 0.5 * (2 ** attempt) + random.uniform(0, 0.25)
            log.warning("embed 429 (attempt %d/%d), sleeping %.2fs",
                        attempt + 1, _OPENAI_MAX_RETRIES + 1, sleep)
            time.sleep(sleep)
    raise RuntimeError("unreachable")


@lru_cache(maxsize=1)
def _embedder_openai_client():
    """Embedder-only OpenAI client.

    Falls back to the shared LLM client unless the model config store or
    ``EMBED_OPENAI_API_KEY`` / ``EMBED_OPENAI_BASE_URL`` are set.
    """
    from openai import OpenAI
    from .openai_client import get_openai_client
    from .model_config_store import get_model_config_store

    store = get_model_config_store()
    embed_api_key = store.embed_api_key or CFG.embed_openai_api_key
    embed_base_url = store.embed_base_url or CFG.embed_openai_base_url

    if not embed_api_key and not embed_base_url:
        return get_openai_client()

    kw: dict = {
        "api_key": embed_api_key or store.llm_api_key or CFG.openai_api_key or "sk-none",
        "base_url": embed_base_url or store.llm_base_url or CFG.openai_base_url,
        "max_retries": 5,
    }
    if CFG.llm_http_timeout and CFG.llm_http_timeout > 0:
        kw["timeout"] = CFG.llm_http_timeout
    return OpenAI(**kw)


def _embed_openai(texts: List[str]) -> List[List[float]]:
    """走 OpenAI 兼容 embeddings 接口；按 10 条/批切，并发 ``EMBED_API_CONCURRENCY`` 个 in-flight。"""
    client = _embedder_openai_client()
    from .model_config_store import get_model_config_store
    store = get_model_config_store()
    model_name = store.embed_model or CFG.embed_model

    safe_texts = [t[:_OPENAI_MAX_INPUT_CHARS] if t else t for t in texts]

    # 准备 chunks：保留 (chunk_idx, chunk) 以便并发返回后按序合并
    chunks: List[tuple[int, List[str]]] = []
    for i in range(0, len(safe_texts), _OPENAI_BATCH_HARD_LIMIT):
        chunks.append((len(chunks), safe_texts[i: i + _OPENAI_BATCH_HARD_LIMIT]))

    if len(chunks) == 1:
        # 单批没必要起线程池
        return _call_one_batch(client, model_name, chunks[0][1])

    results: List[Optional[List[List[float]]]] = [None] * len(chunks)
    workers = min(_OPENAI_CONCURRENCY, len(chunks))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        fut_to_idx = {
            ex.submit(_call_one_batch, client, model_name, ch): idx
            for idx, ch in chunks
        }
        for fut in as_completed(fut_to_idx):
            idx = fut_to_idx[fut]
            results[idx] = fut.result()

    out: List[List[float]] = []
    for batch in results:
        if batch is None:
            raise RuntimeError("embed: missing batch result (concurrency bug)")
        out.extend(batch)
    return out


def warmup_embedding_model() -> None:
    """在启动多线程 worker 前调用；做一次空调用确保连通。"""
    _embed_openai(["ping"])


def embed(texts: List[str]) -> List[List[float]]:
    """批量编码为向量列表；空输入直接返回 ``[]``。"""
    if not texts:
        return []
    return _embed_openai(texts)


def embed_one(text: str) -> List[float]:
    """单条文本编码，内部仍走 batch=1 的 embed。"""
    return embed([text])[0]
