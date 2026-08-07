"""DataBridge 本地状态路径解析。

所有 DataBridge 侧的运行时状态统一归属到 ``${DATAPAW_HOME:-~/.datapaw}/data-bridge/``，
敏感配置归入 ``${DATAPAW_HOME:-~/.datapaw}/.secrets/``。

参考：DataPaw 状态目录统一设计文档。
"""
from __future__ import annotations

import os
from pathlib import Path

DATAPAW_HOME = "DATAPAW_HOME"
_DEFAULT_HOME = Path.home() / ".datapaw"


def resolve_datapaw_home() -> Path:
    """解析 DATAPAW_HOME 根目录。

    优先级：``DATAPAW_HOME`` 环境变量 > 默认 ``~/.datapaw``。
    """
    raw = os.environ.get(DATAPAW_HOME)
    if raw:
        return Path(raw).expanduser().resolve()
    return _DEFAULT_HOME.expanduser().resolve()


def data_bridge_root() -> Path:
    """DataBridge 顶层状态目录。"""
    return resolve_datapaw_home() / "data-bridge"


def data_bridge_cache_dir() -> Path:
    """DataBridge 共享缓存目录。"""
    return data_bridge_root() / "cache"


def data_bridge_kg_dir() -> Path:
    """Knowledge Graph 相关共享状态目录。"""
    return data_bridge_root() / "kg"


def data_bridge_state_dir() -> Path:
    """DataBridge 服务级持久状态目录（durable state / shared）。"""
    return data_bridge_root() / "state"


def data_bridge_logs_dir() -> Path:
    """DataBridge 日志目录。"""
    return data_bridge_root() / "logs"


def secrets_dir() -> Path:
    """跨域敏感配置目录。"""
    return resolve_datapaw_home() / ".secrets"


def semantic_config_db_path() -> Path:
    """``semantic_config.db`` 目标路径。"""
    return data_bridge_state_dir() / "semantic_config.db"


def sessions_db_path() -> Path:
    """``sessions.db`` 目标路径。"""
    return data_bridge_state_dir() / "sessions.db"


def jobs_db_path() -> Path:
    """持久化任务、租约与短期操作计划的 SQLite 数据库。"""
    return data_bridge_state_dir() / "jobs.db"


def embedding_jobs_dir() -> Path:
    """Embedding rebuild jobs 状态文件目录。"""
    return data_bridge_state_dir() / "jobs"


def kg_documents_dir() -> Path:
    """知识图谱文档持久化目录。"""
    return data_bridge_kg_dir() / "documents"


def knowledge_ingest_cache_dir() -> Path:
    """Knowledge ingest 中间缓存目录。"""
    return data_bridge_cache_dir() / "knowledge_ingest"


def knowledge_ingest_report_path() -> Path:
    """最近一次 knowledge ingest 报告文件路径。"""
    return knowledge_ingest_cache_dir() / "knowledge_ingest_report.md"


def models_json_path() -> Path:
    """``models.json`` 目标路径（含 API key，属敏感配置）。"""
    return secrets_dir() / "models.json"


def access_log_path() -> Path:
    """DataBridge HTTP API access log 目标路径。"""
    return data_bridge_logs_dir() / "access.log"


def security_audit_log_path() -> Path:
    """Security decisions and privileged-action audit log path."""
    return data_bridge_logs_dir() / "security_audit.jsonl"


def mcp_access_log_path() -> Path:
    """MCP 工具调用 access log 目标路径。"""
    return data_bridge_logs_dir() / "mcp_access.log"
