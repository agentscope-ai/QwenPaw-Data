"""Consume Auth grants and execute bounded Raw operations on a borrowed driver."""

import os
import time

from qwenpaw_data_auth.audit import log_event
from qwenpaw_data_auth.breakglass import consume_raw_cypher_grant, raw_query_digest
from qwenpaw_data_auth.permissions import check_permission

from ....auth import require_target
from ..cypher import _to_jsonable
from .guards import OWNER_PARAM, RawQueryViolation, admit


class RawCypherService:
    """Compatibility escape hatch; WorkspaceRuntime owns the driver's lifecycle."""

    def __init__(self, driver, target):
        self._driver, self._target = driver, target

    def execute(
        self, *, cypher, params, limit, context, token, control_plane_dsn, actor_ip=""
    ):
        if os.getenv("QWENPAW_DATA_RAW_CYPHER_ENABLED", "0") != "1":
            raise RawQueryViolation("RAW_CYPHER_DISABLED", "Raw Cypher 尚未启用", 404)
        require_target(
            context,
            tenant_id=self._target.tenant_id,
            workspace_id=self._target.workspace_id,
        )
        if not check_permission(context.active_workspace_role, "raw_cypher"):
            raise RawQueryViolation(
                "FORBIDDEN", "当前 Workspace 角色无 Raw Cypher 权限"
            )
        if not 1 <= limit <= 1000:
            raise RawQueryViolation("INVALID_LIMIT", "limit 必须在 1 到 1000 之间", 400)
        is_write, statement, bound = admit(
            cypher, workspace_id=self._target.workspace_id, user_id=context.user_id
        )
        execution_params = {**params, **bound}
        # Only an already-authorized Workspace administrator may explicitly select global ownership.
        if is_write and params.get(OWNER_PARAM) == "__global__":
            execution_params[OWNER_PARAM] = "__global__"
        digest = raw_query_digest(
            cypher, params
        )  # Exactly the request that the grant approved.
        grant = consume_raw_cypher_grant(
            control_plane_dsn,
            token=token,
            actor_user_id=context.user_id,
            tenant_id=self._target.tenant_id,
            workspace_id=self._target.workspace_id,
            query_digest=digest,
        )
        if grant is None:
            raise RawQueryViolation(
                "BREAK_GLASS_REQUIRED", "需要有效且未使用的一次性授权"
            )
        detail = {
            "tenant_id": self._target.tenant_id,
            "workspace_id": self._target.workspace_id,
            "grant_id": grant["id"],
            "query_digest": digest,
            "is_write": is_write,
            "parameter_names": sorted(params),
        }

        def audit(action, **extra):
            log_event(
                action,
                actor_id=context.user_id,
                actor_ip=actor_ip,
                target_type="cypher",
                target_id=self._target.workspace_id,
                detail={**detail, **extra},
                dsn=control_plane_dsn,
            )

        audit("raw_cypher_grant_consumed")
        start = time.monotonic()
        try:
            rows, columns = self._execute(statement, execution_params, limit, is_write)
        except Exception as exc:
            audit(
                "raw_cypher_executed", outcome="failed", error_type=type(exc).__name__
            )
            raise
        elapsed = int((time.monotonic() - start) * 1000)
        audit(
            "raw_cypher_executed",
            outcome="succeeded",
            elapsed_ms=elapsed,
            row_count=len(rows),
        )
        return {
            "rows": _to_jsonable(rows[:limit]),
            "count": len(rows),
            "columns": columns,
            "elapsed_ms": elapsed,
        }

    def _execute(self, statement, params, limit, is_write):
        timeout = max(
            0.1, min(float(os.getenv("QWENPAW_DATA_CYPHER_TIMEOUT_SECONDS", "10")), 60.0)
        )
        options = {"default_access_mode": "WRITE" if is_write else "READ"}
        if self._target.database:
            options["database"] = self._target.database
        with self._driver.session(**options) as session:
            tx = session.begin_transaction(timeout=timeout)
            try:
                result = tx.run(statement, **params)
                columns = list(result.keys())
                rows = [record.data() for record in result.fetch(limit + 1)]
                result.consume()
                tx.commit()
                return rows, columns
            except BaseException:
                tx.rollback()
                raise
            finally:
                tx.close()
