# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path
from typing import Any

from .core import DataPawHost
from .paths import resolve_datapaw_home


class DataPawHostRegistry:
    def __init__(
        self,
        *,
        home: str | Path | None = None,
        model: Any = None,
        workspace: Any = None,
    ) -> None:
        self.home = resolve_datapaw_home(home)
        self.model = model
        self.workspace = workspace
        self._items: dict[str, DataPawHost] = {}
        self._running: set[str] = set()

    def get(
        self,
        *,
        session_id: str,
    ) -> DataPawHost:
        dp = self._items.get(session_id)
        if dp is None:
            dp = DataPawHost(
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
