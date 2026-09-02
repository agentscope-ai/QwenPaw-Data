"""图后端注册表：进程内多后端实例 + 运行时切换。

单例 ``BackendManager`` 持有按名注册的 ``GraphBackend`` 实例，支持在
不重启进程的情况下切换活跃后端。社区版内置 Neo4j；其他后端实现
``GraphBackend`` 接口后可通过 ``get_manager().register()`` 接入。

设计要点：
- 线程安全：``switch`` / ``register`` / ``close_all`` 互斥。
- 切换只换"活跃"指针，已注册的后端实例保留连接池（再次切回是热的）。
- ``active()`` 返回当前活跃后端；``active_or_none()`` 在未初始化时返回 None，
  供 ``graph_session()`` 向后兼容路径使用。
- 单进程单例：用模块级 ``_MANAGER`` + ``get_manager()`` 访问，避免多处实例化。
"""
from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any, Optional

from .base import GraphBackend

if TYPE_CHECKING:
    from ...config import Config

log = logging.getLogger(__name__)

__all__ = [
    "BackendManager",
    "get_manager",
    "init_backend",
    "reset_manager",
]


class BackendManager:
    """进程级图后端管理器（线程安全单例）。

    持有多个已创建的 ``GraphBackend`` 实例并维护一个"活跃"指针，
    供 ``utils.graph_session()`` 使用。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._backends: dict[str, GraphBackend] = {}
        self._active: Optional[str] = None

    # ------------------------------------------------------------------ #
    # 注册 / 注销
    # ------------------------------------------------------------------ #
    def register(self, name: str, backend: GraphBackend) -> None:
        """注册（或覆盖同名）后端实例。

        覆盖时会先关闭旧实例的连接池。若当前活跃后端正是被覆盖的 name，
        活跃指针自动指向新实例。
        """
        with self._lock:
            old = self._backends.get(name)
            if old is not None and old is not backend:
                try:
                    old.close()
                except Exception as exc:  # noqa: BLE001
                    log.warning("关闭旧后端 %s 失败: %s", name, exc)
            self._backends[name] = backend
            if self._active is None:
                self._active = name
            log.info("已注册图后端: %s (active=%s)", name, self._active)

    def unregister(self, name: str) -> None:
        """注销并关闭指定后端。若它是活跃后端，活跃指针回退。"""
        with self._lock:
            backend = self._backends.pop(name, None)
            if backend is None:
                return
            try:
                backend.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("关闭后端 %s 失败: %s", name, exc)
            if self._active == name:
                # 回退到任意一个仍注册的后端，没有则置空
                self._active = next(iter(self._backends), None)
            log.info("已注销图后端: %s (active=%s)", name, self._active)

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def get(self, name: str) -> Optional[GraphBackend]:
        with self._lock:
            return self._backends.get(name)

    def active_name(self) -> Optional[str]:
        with self._lock:
            return self._active

    def active(self) -> GraphBackend:
        """返回当前活跃后端。未初始化时抛 RuntimeError。"""
        with self._lock:
            if self._active is None:
                raise RuntimeError(
                    "BackendManager 尚未初始化：没有已注册的图后端。"
                    " 请在启动时调用 init_backend(CFG)。"
                )
            return self._backends[self._active]

    def active_or_none(self) -> Optional[GraphBackend]:
        """同 ``active()`` 但未初始化时返回 None（供向后兼容路径）。"""
        with self._lock:
            if self._active is None:
                return None
            return self._backends[self._active]

    def names(self) -> list[str]:
        with self._lock:
            return list(self._backends)

    def info(self) -> dict[str, object]:
        """返回可序列化的状态摘要。"""
        with self._lock:
            return {
                "active": self._active,
                "registered": list(self._backends),
            }

    # ------------------------------------------------------------------ #
    # 运行时切换
    # ------------------------------------------------------------------ #
    def switch(self, name: str) -> str:
        """切换活跃后端到已注册的 ``name``。

        Returns:
            切换后的活跃后端名。

        Raises:
            KeyError: ``name`` 未注册。
        """
        with self._lock:
            if name not in self._backends:
                raise KeyError(
                    f"后端 {name!r} 未注册；已注册: {list(self._backends)}"
                )
            prev = self._active
            self._active = name
            log.info("图后端切换: %s → %s", prev, name)
            return name

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def close_all(self) -> None:
        """关闭并清空所有后端实例（供应用 shutdown）。"""
        with self._lock:
            names = list(self._backends)
            for name in names:
                backend = self._backends.pop(name, None)
                if backend is None:
                    continue
                try:
                    backend.close()
                except Exception as exc:  # noqa: BLE001
                    log.warning("关闭后端 %s 失败: %s", name, exc)
            self._active = None
            log.info("已关闭全部图后端: %s", names)


# ---------------------------------------------------------------------- #
# 模块级单例
# ---------------------------------------------------------------------- #
_MANAGER: Optional[BackendManager] = None
_MANAGER_LOCK = threading.Lock()


def get_manager() -> BackendManager:
    """获取（惰性创建）进程级 BackendManager 单例。"""
    global _MANAGER
    if _MANAGER is None:
        with _MANAGER_LOCK:
            if _MANAGER is None:
                _MANAGER = BackendManager()
    return _MANAGER


def reset_manager() -> None:
    """重置单例（仅供测试）。会关闭所有已注册后端。"""
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is not None:
            _MANAGER.close_all()
        _MANAGER = None


def init_backend(
    cfg: "Config",
    *,
    name: Optional[str] = None,
    neo4j_driver: Any = None,
) -> str:
    """按配置创建后端并注册到 manager。

    Args:
        cfg: Config 实例。
        name: 注册名；默认用 ``cfg.graph_backend``（社区版内置 'neo4j'）。
        neo4j_driver: 外部已创建的 Neo4j driver（如 server lifespan 的
            driver）。传入时 Neo4jBackend 复用而不新建，且不拥有其
            生命周期，避免与注入方双重关闭。

    Returns:
        注册名。
    """
    from . import get_backend

    reg_name = (name or cfg.graph_backend).lower()
    backend = get_backend(cfg, neo4j_driver=neo4j_driver)
    mgr = get_manager()
    mgr.register(reg_name, backend)
    # 首次注册自动成为活跃后端；后续显式 switch 才切。
    if mgr.active_name() is None or reg_name == cfg.graph_backend:
        mgr.switch(reg_name)
    return reg_name
