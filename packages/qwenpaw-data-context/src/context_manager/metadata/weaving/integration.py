"""Adapt editor routes, snapshots and SQL commits for graph publication."""

from fastapi import APIRouter, HTTPException, Request
from semantic_config import tables
from semantic_config.db import get_db
from semantic_config.engine import get_session_factory
from sqlalchemy import select

from .projection import members

EDITOR_TABLES = (
    tables.domain,
    tables.dataset,
    tables.dataset_field,
    tables.dimension_dict,
    tables.dataset_dimension,
    tables.metric_dict,
    tables.metric_caliber,
    tables.verified_query,
)


EDITOR_ENTITIES = frozenset(
    {
        "biz-domain",
        "dataset-meta",
        "dataset-field",
        "dimension",
        "dataset-dimension",
        "metric-lib",
        "metric-caliber",
        "verified-query",
        "dataset-column-meta",
        "metric-formula-lib",
    }
)


async def capture(
    db, *, tenant_id: str, workspace_id: str
) -> dict[str, tuple[dict, ...]]:
    """Read live editor facts within the caller's transaction and workspace."""
    snapshot = {}
    for table in EDITOR_TABLES:
        rows = await db.execute(
            select(table)
            .where(
                table.c.tenant_id == tenant_id,
                table.c.workspace_id == workspace_id,
                table.c.is_deleted == 0,
            )
            .order_by(table.c.id)
        )
        snapshot[table.name] = tuple(dict(row) for row in rows.mappings())
    # Datasource anchors only need display metadata.
    table = tables.datasource
    rows = await db.execute(
        select(
            table.c.datasource_id, table.c.datasource_name, table.c.datasource_type
        ).where(
            table.c.tenant_id == tenant_id,
            table.c.workspace_id == workspace_id,
            table.c.is_deleted == 0,
        )
    )
    snapshot[table.name] = tuple(dict(row) for row in rows.mappings())
    return snapshot


async def complete_snapshot(request, scope):
    """Supply current Schema fields omitted by the editor's snapshot assembler."""
    async with get_session_factory()() as db:
        rows = await db.execute(
            select(tables.domain).where(
                tables.domain.c.tenant_id == scope.tenant_id,
                tables.domain.c.workspace_id == scope.workspace_id,
                tables.domain.c.is_deleted == 0,
            )
        )
        scopes = {
            str(row.get("import_id") or "").strip() or str(row["id"]): members(
                row["datasource_scope"]
            )
            for row in rows.mappings()
        }
    domains = [
        domain.model_copy(update={"datasource_scope": scopes[domain.domain_id]})
        if domain.domain_id in scopes
        else domain
        for domain in request.semantic.domains
    ]
    return request.model_copy(
        update={
            "semantic": request.semantic.model_copy(update={"domains": domains}),
        }
    )


def mount_editor(app):
    """Reuse editor routes, middleware and errors; prefer CM's explicit overrides."""
    from semantic_config.integration import mount_semantic_config
    from semantic_config.routers import explore_router, import_router, weave_task_router

    existing = list(app.router.routes)
    overrides = {
        ("/api/semantic-config/import/excel", "POST"),
        ("/api/semantic-config/weave-task/submit", "POST"),
        ("/api/semantic-config/weave-task", "GET"),
        ("/api/semantic-config/weave-task/{task_id}", "GET"),
        ("/api/semantic-config/weave-task/{task_id}/kill", "POST"),
    }
    mount_semantic_config(app)
    app.include_router(explore_router.router)
    # Only select this app's mounted routes; upstream router objects stay untouched.
    added = [
        route
        for route in app.router.routes[len(existing) :]
        if getattr(route, "original_router", None)
        not in (import_router.router, weave_task_router.router)
    ]
    app.router.routes[:] = existing + added
    for source in (import_router.router, weave_task_router.router):
        selected = APIRouter(
            routes=[
                route
                for route in source.routes
                if not any(
                    (route.path, method) in overrides for method in route.methods
                )
            ]
        )
        app.include_router(selected)


def install_publication(app, publisher_for_request):
    """API supplies authorization; this adapter owns commit-before-publish ordering."""

    async def publishing_db(request: Request):
        parts = (
            request.url.path.removeprefix("/api/semantic-config/").strip("/").split("/")
        )
        entity = parts[0]
        editor_write = (
            request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and entity in EDITOR_ENTITIES
        )
        # Publish formal promotions, not staging edits; standalone Profiling has no graph Runtime.
        profiling_completion = (
            request.method == "POST"
            and len(parts) == 4
            and entity == "probe"
            and parts[-1] == "complete"
            and getattr(request.app.state, "context_runtime", None) is not None
        )
        if not (editor_write or profiling_completion):
            async for db in get_db(request):
                yield db
            return

        publisher, scope = publisher_for_request(request)
        db = get_session_factory()()
        try:
            before = await capture(
                db, tenant_id=scope.tenant_id, workspace_id=scope.workspace_id
            )

            async def finish(exc_type, exc, traceback):
                if exc_type is not None:
                    await db.rollback()
                    return False
                after = await capture(
                    db, tenant_id=scope.tenant_id, workspace_id=scope.workspace_id
                )
                await db.commit()
                if before != after:
                    try:
                        await publisher.publish_changes(
                            before, after, scope, entity=entity
                        )
                    except Exception as error:
                        raise HTTPException(
                            503,
                            "配置已保存，但图同步失败；请刷新确认已保存内容，并通过语义编织重新发布。",
                        ) from error
                return False

            # The overridden ``get_db`` dependency is function-scoped.  Its
            # teardown already runs before the response is sent, so finish the
            # transaction and publish inline while the session is still open.
            yield db
            await finish(None, None, None)
        except BaseException:
            await db.rollback()
            raise
        finally:
            await db.close()

    app.dependency_overrides[get_db] = publishing_db
