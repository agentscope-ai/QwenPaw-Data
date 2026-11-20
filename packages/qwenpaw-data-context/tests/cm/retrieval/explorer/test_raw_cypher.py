"""Exercise Raw admission, real one-time Auth grants and transaction outcomes."""

import json
from types import SimpleNamespace

import pytest
from context_manager.compatibility.explorer.raw_cypher import (
    RawCypherService,
    RawQueryViolation,
)
from context_manager.compatibility.explorer.raw_cypher.guards import admit
from context_manager.store.neo4j import GraphTarget
from qwenpaw_data_auth.breakglass import issue_raw_cypher_grant, raw_query_digest
from qwenpaw_data_auth.database import get_db, init_schema
from neo4j import Record

from cm.infrastructure.access.support import authorization

PROPS = "{workspace_id: $__raw_workspace_id, owner_id: $__raw_owner_id}"
WRITE = f"CREATE (n:Entity {PROPS}) RETURN n"


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n) DELETE n",
        f"MATCH (n:Dataset {PROPS}) SET n.description = 'x'",
        f"MATCH (n:Entity {PROPS}) SET n.owner_id = 'bob'",
        f"MATCH (n:Entity {PROPS}) SET n += $props",
        f"MATCH (n:Entity {PROPS}) SET n:Dataset",
        f"MATCH (n:Entity {PROPS}) REMOVE n:Entity",
        f"MATCH (n:Entity {PROPS}) DETACH DELETE n",
        f"MATCH (n:Entity {PROPS}) WITH n SET n.name = 'x'",
        f"MATCH (n:Entity {PROPS}) SET n.name = apoc.text.join(['a'], '')",
        "CREATE (:Entity {name: 'x'})",
        f"CREATE (n:Claim {PROPS}) RETURN n",
        f"CREATE (n:Strategy {PROPS}) RETURN n",
        "CREATE (n:Entity $props)",
        f"CREATE (n:Entity {PROPS}) RETURN n; MATCH (x) RETURN x",
        "CALL db.labels()",
        "RETURN apoc.cypher.runFirstColumn('MATCH (n) RETURN n', {}, true)",
        "MATCH (n) RETURN custom.readOtherWorkspace(n)",
        "USE other MATCH (n) RETURN n",
        f"MATCH (n:Entity {PROPS}) SET n.name = 'x' RETURN [(n)-->(m) | m]",
        f"MATCH (n:Entity {PROPS}) WHERE EXISTS {{ MATCH (m) RETURN m }} SET n.name = 'x'",
        f"MATCH (n:Entity {PROPS}) CREATE (n)-[:UNKNOWN]->(n)",
    ],
)
def test_rejects_unscoped_or_protected_mutations(query):
    with pytest.raises(RawQueryViolation):
        admit(query, workspace_id="w1", user_id="u1")


class Result:
    def __init__(self, rows=()):
        self.rows = [Record(row) for row in rows]

    def keys(self):
        return self.rows[0].keys() if self.rows else []

    def fetch(self, count):
        return self.rows[:count]

    def consume(self):
        return SimpleNamespace()


class Driver:
    def __init__(self):
        self.calls = []
        self.committed = False
        self.rolled_back = False
        self.fail = False

    def session(self, **kwargs):
        self.options = kwargs
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def begin_transaction(self, **kwargs):
        return self

    def run(self, query, **params):
        self.calls.append((query, params))
        if self.fail:
            raise RuntimeError("backend failed")
        return Result([{"value": 1}])

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


@pytest.fixture
def raw(tmp_path, monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_RAW_CYPHER_ENABLED", "1")
    dsn = f"sqlite:///{tmp_path / 'raw-auth.db'}"
    init_schema(dsn)
    with get_db(dsn) as conn:
        for sql in (
            "INSERT INTO platform_admins (id, username, password_hash) VALUES ('p1', 'platform', 'x')",
            "INSERT INTO tenants (id, name, neo4j_database) VALUES ('t1', 'Tenant', 'neo4j')",
            "INSERT INTO workspaces (id, tenant_id, name, instance_status) VALUES ('w1', 't1', 'WS', 'ready')",
            "INSERT INTO users (id, tenant_id, username) VALUES ('u1', 't1', 'admin')",
        ):
            conn.execute(sql)
        conn.commit()
    context = authorization("u1", "t1", "w1", role="ws_admin")
    driver = Driver()
    service = RawCypherService(driver, GraphTarget("t1", "w1", database="graph-db"))

    def request(query=WRITE, params=None):
        params = params or {}
        token, _ = issue_raw_cypher_grant(
            dsn,
            platform_admin_id="p1",
            actor_user_id="u1",
            tenant_id="t1",
            workspace_id="w1",
            query_digest=raw_query_digest(query, params),
            ttl_seconds=120,
        )
        return {
            "cypher": query,
            "params": params,
            "limit": 10,
            "context": context,
            "token": token,
            "control_plane_dsn": dsn,
        }

    return service, driver, request, dsn


def test_grant_is_single_use_and_scope_params_cannot_be_forged(raw):
    service, driver, request, dsn = raw
    args = request(params={"__raw_workspace_id": "other", "__raw_owner_id": "other"})
    assert service.execute(**args)["rows"] == [{"value": 1}]
    assert driver.committed and driver.options["database"] == "graph-db"
    executed = next(params for query, params in driver.calls if query == WRITE)
    assert executed["__raw_workspace_id"] == "w1" and executed["__raw_owner_id"] == "u1"
    with pytest.raises(RawQueryViolation) as err:
        service.execute(**args)
    assert err.value.code == "BREAK_GLASS_REQUIRED"
    with get_db(dsn) as conn:
        rows = conn.execute(
            "SELECT event_type, detail FROM audit_logs ORDER BY timestamp"
        ).fetchall()
    assert [r[0] for r in rows] == ["raw_cypher_grant_consumed", "raw_cypher_executed"]
    assert json.loads(rows[1][1])["row_count"] == 1
