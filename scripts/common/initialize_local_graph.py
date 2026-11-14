"""Explicitly prepare a fixed local graph before starting application processes.

Registered Workspaces must use workspace_provisioner.py --initialize-workspace.
"""

import argparse
from contextlib import closing

from context_manager.settings import get_settings
from context_manager.store.neo4j import GraphTarget, Neo4jRuntime
from context_manager.store.neo4j.physical_schema import (
    ensure_schema as ensure_neo4j_schema,
)
from neo4j import GraphDatabase


def main() -> None:
    argparse.ArgumentParser(description=__doc__).parse_args()
    settings = get_settings()
    route = settings.runtime
    if settings.graph.backend == "holoage":
        # Holo AGE is opt-in: import its driver stack only when it is selected.
        from context_manager.store.holoage import HoloDriver, HoloRuntime
        from context_manager.store.holoage.physical_schema import build_physical_schema
        from context_manager.store.holoage.physical_schema import (
            ensure_schema as ensure_holo_schema,
        )
        from context_manager.store.holoage.target import HoloTarget

        with closing(
            HoloDriver(connect_timeout=15, **settings.graph.connection_params())
        ) as driver:
            runtime = HoloRuntime(
                driver,
                HoloTarget(
                    route.tenant_id, route.workspace_id, settings.graph.graph_name
                ),
            )
            runtime.prepare_schema(
                lambda execute: ensure_holo_schema(
                    settings.graph.graph_name,
                    build_physical_schema(
                        embed_dim=settings.embedding.dimensions or 1024
                    ),
                    execute,
                )
            )
    else:
        with closing(
            GraphDatabase.driver(
                route.neo4j_uri, auth=(route.neo4j_user, route.neo4j_password)
            )
        ) as driver:
            ensure_neo4j_schema(
                Neo4jRuntime(
                    driver,
                    GraphTarget(
                        route.tenant_id,
                        route.workspace_id,
                        database=route.neo4j_database,
                    ),
                )
            )
    print("Local graph schema is ready")


if __name__ == "__main__":
    main()
