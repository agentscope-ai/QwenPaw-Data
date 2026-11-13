"""Record incremental graph outcomes in the shared relational database."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import insert
from sqlalchemy.engine import Engine

from ...schema.relational import sync_log, sync_change


class WeavingRepository:
    """Persist one batch and its ordered changes after graph commit or failure."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(self, task_id, entity, scope, changes, error, *, skipped_dataset_ids):
        timestamp = datetime.now(timezone.utc).isoformat()
        audit = dict(
            tenant_id=scope.tenant_id,
            workspace_id=scope.workspace_id,
            created_by=scope.user_id,
            updated_by=scope.user_id,
            created_at=timestamp,
            updated_at=timestamp,
            is_deleted=0,
        )
        with self._engine.begin() as db:
            result = db.execute(
                insert(sync_log).values(
                    task_id=task_id,
                    entity=entity,
                    status="failed" if error else "success",
                    applied=0 if error else len(changes),
                    change_count=len(changes),
                    skipped=json.dumps(
                        [
                            {
                                "dataset_id": dataset_id,
                                "reason": "missing_datasource_id",
                            }
                            for dataset_id in sorted(skipped_dataset_ids)
                        ]
                    ),
                    errors=json.dumps([error] if error else [], ensure_ascii=False),
                    **audit,
                )
            )
            log_id = result.inserted_primary_key[0]
            if changes:
                db.execute(
                    insert(sync_change),
                    [
                        dict(
                            sync_log_id=log_id,
                            task_id=task_id,
                            seq=index,
                            op=op,
                            sheet=sheet,
                            graph_key=key,
                            result="failed" if error else "applied",
                            reason=error,
                            **audit,
                        )
                        for index, (op, sheet, key) in enumerate(changes)
                    ],
                )
