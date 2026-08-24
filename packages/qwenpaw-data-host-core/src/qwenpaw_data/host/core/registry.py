# -*- coding: utf-8 -*-
from __future__ import annotations

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
    ) -> None:
        self.home = resolve_qwenpaw_data_home(home)
        self.model = model
        self.workspace = workspace
        self._items: dict[str, QwenPawDataHost] = {}
        self._running: set[str] = set()

    def get(
        self,
        *,
        session_id: str,
    ) -> QwenPawDataHost:
        dp = self._items.get(session_id)
        if dp is None:
            dp = QwenPawDataHost(
                home=self.home,
                model=self.model,
                workspace=self.workspace,
                session_id=session_id,
            )
            self._items[session_id] = dp
        return dp

    def is_running(
        self,
        *,
        session_id: str,
    ) -> bool:
        return session_id in self._running
