"""模型配置持久化存储：读写 models.json、API Key 掩码、连接测试、客户端缓存失效。

存储路径由 ``MODEL_CONFIG_PATH`` 环境变量指定，默认 ``$QWENPAW_DATA_HOME/.secrets/models.json``。
"""
from __future__ import annotations

import json
import logging
import os
import stat
import threading
from pathlib import Path
from typing import Any, Optional

from .config import CFG
from qwenpaw_data.context.paths import models_json_path as _default_models_json

log = logging.getLogger("context_manager.model_config_store")

_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_PATH = _default_models_json()
_EMBEDDING_FIELDS = ("model", "base_url", "api_key", "dim")


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) > 8:
        return key[:4] + "****" + key[-4:]
    return "****"


def _embedding_config(data: Any) -> dict[str, Any]:
    """Return only fields supported by the remote embedding contract."""
    if not isinstance(data, dict):
        return {}
    return {key: data[key] for key in _EMBEDDING_FIELDS if key in data}


def _initial_from_env() -> dict[str, Any]:
    return {
        "llm": {
            "base_url": CFG.openai_base_url,
            "model": CFG.llm_model,
            "api_key": CFG.openai_api_key,
        },
        "embedding": {
            "model": CFG.embed_model,
            # Fall back to shared LLM endpoint/key when EMBED_OPENAI_* is unset.
            "base_url": CFG.embed_openai_base_url or CFG.openai_base_url,
            "api_key": CFG.embed_openai_api_key or CFG.openai_api_key,
            "dim": CFG.embed_dim,
        },
    }


class ModelConfigStore:
    """进程内单例，管理 models.json 的读写与客户端缓存刷新。"""

    def __init__(self, path: Optional[Path] = None):
        raw = (os.getenv("MODEL_CONFIG_PATH") or "").strip()
        self._path = Path(raw) if raw else (path or _DEFAULT_PATH)
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if self._path.is_file():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
                self._data["embedding"] = _embedding_config(self._data.get("embedding"))
                log.info("Loaded model config from %s", self._path)
                return
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("Failed to read %s, re-initializing: %s", self._path, exc)
        self._data = _initial_from_env()
        self._save()
        log.info("Initialized model config from env vars → %s", self._path)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._path)
        try:
            os.chmod(self._path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError:
            pass

    # ---- public read ----

    def load(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def get_masked(self) -> dict[str, Any]:
        with self._lock:
            data = json.loads(json.dumps(self._data))
        llm = data.get("llm") or {}
        emb = _embedding_config(data.get("embedding"))
        llm["api_key"] = _mask_key(llm.get("api_key", ""))
        emb["api_key"] = _mask_key(emb.get("api_key", ""))
        return {"llm": llm, "embedding": emb}

    # ---- public write ----

    def update_llm(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            llm = dict(self._data.get("llm") or {})
            for k in ("base_url", "model"):
                if k in payload and payload[k] is not None:
                    llm[k] = payload[k]
            if payload.get("api_key"):
                llm["api_key"] = payload["api_key"]
            self._data["llm"] = llm
            self._save()
        self._invalidate_llm_client()
        log.info("LLM config updated")
        return self.get_masked()["llm"]

    def update_embedding(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """返回 (masked_embedding_config, rebuild_required)。"""
        with self._lock:
            old = _embedding_config(self._data.get("embedding"))
            emb = dict(old)
            for k in ("model", "base_url"):
                if k in payload and payload[k] is not None:
                    emb[k] = payload[k]
            if "dim" in payload and payload["dim"] is not None:
                emb["dim"] = int(payload["dim"])
            if payload.get("api_key"):
                emb["api_key"] = payload["api_key"]
            self._data["embedding"] = emb
            self._save()

            rebuild = any(
                old.get(k) != emb.get(k)
                for k in ("model", "base_url", "dim")
            )

        self._invalidate_embedding_client()
        log.info("Embedding config updated (rebuild_required=%s)", rebuild)
        return self.get_masked()["embedding"], rebuild

    # ---- property accessors (for openai_client / embedder) ----

    @property
    def llm_api_key(self) -> str:
        return (self._data.get("llm") or {}).get("api_key", "") or ""

    @property
    def llm_base_url(self) -> str:
        return (self._data.get("llm") or {}).get("base_url", "") or ""

    @property
    def llm_model(self) -> str:
        return (self._data.get("llm") or {}).get("model", "") or ""

    @property
    def embed_model(self) -> str:
        return (self._data.get("embedding") or {}).get("model", "") or ""

    @property
    def embed_base_url(self) -> str:
        return (self._data.get("embedding") or {}).get("base_url", "") or ""

    @property
    def embed_api_key(self) -> str:
        return (self._data.get("embedding") or {}).get("api_key", "") or ""

    @property
    def embed_dim(self) -> int:
        return int((self._data.get("embedding") or {}).get("dim", 1024))

    # ---- connection tests ----

    def test_llm(self, config: dict[str, Any]) -> dict[str, Any]:
        try:
            from openai import OpenAI
            client = OpenAI(
                api_key=config.get("api_key") or self.llm_api_key or "sk-none",
                base_url=config.get("base_url") or self.llm_base_url,
                timeout=15.0,
                max_retries=0,
            )
            model = config.get("model") or self.llm_model or "gpt-4o-mini"
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=5,
            )
            return {"success": True, "message": f"Connected. Model: {resp.model}"}
        except Exception as exc:
            return {"success": False, "message": str(exc)}

    def test_embedding(self, config: dict[str, Any]) -> dict[str, Any]:
        try:
            from openai import OpenAI
            api_key = config.get("api_key") or self.embed_api_key or self.llm_api_key or "sk-none"
            base_url = config.get("base_url") or self.embed_base_url or self.llm_base_url
            client = OpenAI(api_key=api_key, base_url=base_url, timeout=15.0, max_retries=0)
            model = config.get("model") or self.embed_model
            resp = client.embeddings.create(model=model, input=["test"])
            dim = len(resp.data[0].embedding)
            return {"success": True, "message": f"Connected. Dimension: {dim}", "detected_dim": dim}
        except Exception as exc:
            return {"success": False, "message": str(exc), "detected_dim": None}

    # ---- cache invalidation ----

    def _invalidate_llm_client(self) -> None:
        try:
            from .openai_client import get_openai_client
            get_openai_client.cache_clear()
            log.info("LLM client cache cleared")
        except Exception as exc:
            log.warning("Failed to clear LLM client cache: %s", exc)

    def _invalidate_embedding_client(self) -> None:
        try:
            from .embedder import _embedder_openai_client
            _embedder_openai_client.cache_clear()
            log.info("Embedding client cache cleared")
        except Exception as exc:
            log.warning("Failed to clear embedding client cache: %s", exc)


_store_instance: Optional[ModelConfigStore] = None
_store_lock = threading.Lock()


def get_model_config_store() -> ModelConfigStore:
    global _store_instance
    if _store_instance is not None:
        return _store_instance
    with _store_lock:
        if _store_instance is not None:
            return _store_instance
        _store_instance = ModelConfigStore()
        return _store_instance
