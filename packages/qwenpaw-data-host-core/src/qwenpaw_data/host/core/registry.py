# -*- coding: utf-8 -*-
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .core import QwenPawDataHost
from .paths import resolve_qwenpaw_data_home


class QwenPawDataHostRegistry:
    def __init__(
        self,
        *,
        home: str | Path | None = None,
        model: Any = None,
        workspace: Any = None,
        extra_middlewares_factory: Callable[[], list[Any]] | None = None,
        model_factory: Any = None,
    ) -> None:
        self.home = resolve_qwenpaw_data_home(home)
        self.model = model
        self.workspace = workspace
        self.extra_middlewares_factory = extra_middlewares_factory
        self.model_factory = model_factory
        self._items: dict[str, QwenPawDataHost] = {}
        self._running: set[str] = set()

    def get(
        self,
        *,
        session_id: str,
    ) -> QwenPawDataHost:
        dp = self._items.get(session_id)
        if dp is None:
            extra_middlewares = (
                self.extra_middlewares_factory()
                if self.extra_middlewares_factory is not None
                else None
            )
            dp = QwenPawDataHost(
                home=self.home,
                model=self.model,
                workspace=self.workspace,
                session_id=session_id,
                extra_middlewares=extra_middlewares,
                model_factory=self.model_factory,
            )
            self._items[session_id] = dp
        return dp

    def is_running(
        self,
        *,
        session_id: str,
    ) -> bool:
        return session_id in self._running
