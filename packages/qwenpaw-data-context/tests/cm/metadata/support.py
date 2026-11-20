"""Metadata imports share the service graph fake with explicit create/patch semantics."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from context_manager.schema.relational import recovery_runs
from context_manager.transaction import Transaction
from semantic_config.engine import get_session_factory
from sqlalchemy import insert

from cm.knowledge.support import MemoryGraphStore  # noqa: F401


class UnstackedImports:
    """Isolate plan/model tests from recovery; earlier batches remain committed.

    Public recovery behavior is covered by infrastructure/storage tests using
    RecoveryService, SQL receipts and real Neo4j transactions.
    """

    def __init__(self, store):
        self.execute = Transaction(store).execute

    async def require_ready(self, scope):
        pass

    @asynccontextmanager
    async def run(self, scope, *, task_type, task_id=None, rid=None):
        result = {"result_status": "SUCCESS", "message": None}
        session = SimpleNamespace(
            execute=self.execute,
            set_result=lambda status, message=None: result.update(
                result_status=status, message=message
            ),
        )
        try:
            yield session
        except (Exception, asyncio.CancelledError) as exc:
            result.update(
                result_status="KILLED"
                if isinstance(exc, asyncio.CancelledError)
                else "FAILED",
                message=str(exc) or None,
            )
            raise
        finally:
            # Task tests consume SQL receipts; standalone plan tests need no SQL.
            if task_id is not None:
                async with get_session_factory()() as db, db.begin():
                    await db.execute(
                        insert(recovery_runs).values(
                            tenant_id=scope.tenant_id,
                            workspace_id=scope.workspace_id,
                            task_type=task_type,
                            task_id=task_id,
                            rid=rid,
                            recovery_state="CLOSED",
                            **result,
                        )
                    )
