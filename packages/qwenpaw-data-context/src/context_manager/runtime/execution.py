"""Assemble and close the execution budgets owned by one Runtime."""

import asyncio

from ..executor import BoundedExecutor, drain
from ..settings.execution import ExecutionSettings


class ExecutionResources:
    """Own one Runtime's execution budgets, shared across its Workspaces."""

    def __init__(self, settings: ExecutionSettings):
        self.graph_read = BoundedExecutor("graph_read", settings.graph_read)
        self.background = BoundedExecutor("background", settings.background)
        self.sql = BoundedExecutor("sql", settings.sql)

    def close(self):
        self.graph_read.close()
        self.background.close()
        self.sql.close()

    async def aclose(self):
        await drain(
            asyncio.gather(
                self.graph_read.aclose(),
                self.background.aclose(),
                self.sql.aclose(),
            )
        )
