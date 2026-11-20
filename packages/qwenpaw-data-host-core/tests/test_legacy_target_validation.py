"""Target preflight must not bootstrap accounts or mutate an existing database."""

from dataclasses import replace
import hashlib
import json

import pytest
from sqlalchemy import create_engine, insert, text

from qwenpaw_data_auth.database import metadata as auth_metadata
from qwenpaw_data.host.core.db.tables import Base
from qwenpaw_data.host.core.migration.legacy_json import MigrationError
from qwenpaw_data.host.core.migration.plan import PlanIssue
from qwenpaw_data.host.core.migration.target import _read_only_target, validate_target

from .test_legacy_migration_plan import prepare, source as make_source, write
from .test_legacy_record_migration import USERS


@pytest.fixture
def source(tmp_path):
    return make_source.__wrapped__(tmp_path)


def initialize_target(path, identity=None):
    identity = identity or USERS["old"]
    url = f"sqlite:///{path}"
    engine = create_engine(url)
    auth_metadata.create_all(engine)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            insert(auth_metadata.tables["tenants"]),
            dict(
                id=identity["tenant_id"],
                name="tenant",
                neo4j_database="neo4j",
            ),
        )
        conn.execute(
            insert(auth_metadata.tables["workspaces"]),
            dict(
                id=identity["workspace_id"],
                tenant_id=identity["tenant_id"],
                name="workspace",
            ),
        )
        conn.execute(
            insert(auth_metadata.tables["users"]),
            dict(
                id=identity["user_id"],
                tenant_id=identity["tenant_id"],
                username="user",
            ),
        )
        conn.execute(
            insert(auth_metadata.tables["role_bindings"]),
            dict(
                id="binding",
                user_id=identity["user_id"],
                role="member",
                scope_type="workspace",
                scope_id=identity["workspace_id"],
            ),
        )
    engine.dispose()
    return url


@pytest.fixture
def target(tmp_path):
    path = tmp_path / "target # space.sqlite"
    return path, initialize_target(path)


def mutate(url, statement, params=None):
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text(statement), params or {})
    finally:
        engine.dispose()


def test_full_preflight_reads_all_tables_without_writes_or_secret_output(
    source, target
):
    plan = prepare(source)
    path, url = target
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    files = set(path.parent.rglob("*"))
    check = validate_target(plan, database_url=url, target_stopped=True)
    assert check.issues == ()
    assert check.rows_checked == len(plan.rows)
    assert check.identities_checked == 1
    assert check.report()["target_checks_passed"] is True
    assert check.report()["import_ready"] is False
    assert check.report()["target_master_key_provisioned"] is False
    assert check.report()["sql_written"] is False
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert set(path.parent.rglob("*")) == files
    assert not plan.target_home.exists()
    assert "synthetic-private-key" not in json.dumps(check.report()) + repr(check)
    assert str(path) not in json.dumps(check.report())


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE users SET status='inactive'",
        "UPDATE tenants SET status='inactive'",
        "UPDATE workspaces SET status='inactive'",
        "DELETE FROM role_bindings",
        "UPDATE role_bindings SET role='invented'",
        "UPDATE role_bindings SET scope_type='tenant'",  # member is not tenant admin
        "UPDATE role_bindings SET scope_id='another-workspace'",
        "UPDATE users SET tenant_id='another-tenant'",
        "UPDATE workspaces SET tenant_id='another-tenant'",
        "UPDATE role_bindings SET role='tenant_admin', scope_type='tenant', scope_id='other'",
    ],
)
def test_missing_inactive_or_cross_tenant_memberships_fail_closed(
    source, target, statement
):
    path, url = target
    mutate(url, statement)
    before = path.read_bytes()
    check = validate_target(prepare(source), database_url=url, target_stopped=True)
    assert [i.code for i in check.issues] == ["target_identity_not_authorized"]
    assert check.report()["target_checks_passed"] is False
    assert path.read_bytes() == before


@pytest.mark.parametrize("role", ["member", "ws_admin", "tenant_admin"])
def test_native_auth_roles_are_accepted_without_requiring_redundant_membership(
    source, target, role
):
    from qwenpaw_data_auth.authentication import resolve_current_authorization
    from qwenpaw_data_auth.database import reset_engines

    _, url = target
    identity = USERS["old"]
    mutate(
        url,
        "UPDATE role_bindings SET role=:role, scope_type=:scope, scope_id=:id",
        dict(
            role=role,
            scope="tenant" if role == "tenant_admin" else "workspace",
            id=identity["tenant_id"]
            if role == "tenant_admin"
            else identity["workspace_id"],
        ),
    )
    assert not validate_target(
        prepare(source), database_url=url, target_stopped=True
    ).issues
    # The runtime resolver configures WAL, which is precisely why preflight
    # must not call it. Compare afterwards, then close its fixture connections.
    try:
        assert (
            resolve_current_authorization(
                url,
                identity["user_id"],
                identity["tenant_id"],
                identity["workspace_id"],
            )[1]
            == role
        )
    finally:
        reset_engines()


def test_existing_primary_keys_in_every_prepared_table_are_never_overwritten(
    source, target
):
    path, url = target
    plan = prepare(source)
    engine = create_engine(url)
    with engine.begin() as conn:
        for row in plan.rows:
            conn.execute(insert(Base.metadata.tables[row.table]), row.values)
    engine.dispose()
    before = path.read_bytes()
    check = validate_target(plan, database_url=url, target_stopped=True)
    assert {i.path for i in check.issues} == {row.table for row in plan.rows}
    assert all(i.code == "target_primary_key_conflict" for i in check.issues)
    assert path.read_bytes() == before


def test_preferences_only_users_still_require_workspace_authorization(source, target):
    plan = prepare(source)
    plan = replace(
        plan, rows=tuple(r for r in plan.rows if r.table.startswith("user_"))
    )
    _, url = target
    mutate(url, "DELETE FROM role_bindings")
    assert (
        validate_target(plan, database_url=url, target_stopped=True).issues[0].code
        == "target_identity_not_authorized"
    )


def test_empty_preferences_user_is_not_lost_when_no_rows_are_prepared(source, target):
    write(source, "host/preferences/second.json", {})
    users = {**USERS, "second": {**USERS["old"], "user_id": "second-target"}}
    plan = prepare(source, users=users)
    assert not plan.issues
    assert {identity.user_id for identity in plan.identities} == {
        USERS["old"]["user_id"],
        "second-target",
    }
    assert not any(row.values["user_id"] == "second-target" for row in plan.rows)
    check = validate_target(plan, database_url=target[1], target_stopped=True)
    assert len(check.issues) == 1
    assert check.issues[0].path == "identities/second-target"


@pytest.mark.parametrize(
    "statement",
    [
        "DROP TABLE users",
        "DROP TABLE sessions",
        "ALTER TABLE chats DROP COLUMN artifact_comments_json",
        "DROP TABLE sessions; CREATE TABLE sessions AS SELECT 1 AS id",
    ],
)
def test_schema_is_not_created_or_repaired(source, target, statement):
    path, url = target
    for part in statement.split(";"):
        mutate(url, part)
    before = path.read_bytes()
    with pytest.raises(MigrationError, match="schema"):
        validate_target(prepare(source), database_url=url, target_stopped=True)
    assert path.read_bytes() == before


def test_missing_target_is_not_created(source, tmp_path):
    path = tmp_path / "absent.sqlite"
    with pytest.raises(MigrationError, match="existing"):
        validate_target(
            prepare(source), database_url=f"sqlite:///{path}", target_stopped=True
        )
    assert not path.exists()


def test_native_columns_without_native_primary_key_are_rejected(source, target):
    path, url = target
    for statement in (
        "CREATE TABLE sessions_without_key AS SELECT * FROM sessions",
        "DROP TABLE sessions",
        "ALTER TABLE sessions_without_key RENAME TO sessions",
    ):
        mutate(url, statement)
    before = path.read_bytes()
    with pytest.raises(MigrationError, match="primary key"):
        validate_target(prepare(source), database_url=url, target_stopped=True)
    assert path.read_bytes() == before


def test_bulk_conflict_checks_bound_parameter_counts(source, target):
    _, url = target
    plan = prepare(source)
    session = next(row for row in plan.rows if row.table == "sessions")
    records = tuple(
        replace(session, values={**session.values, "id": f"session-{i}"})
        for i in range(1001)
    )
    plan = replace(plan, rows=records)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            insert(Base.metadata.tables["sessions"]), [row.values for row in records]
        )
    engine.dispose()
    check = validate_target(plan, database_url=url, target_stopped=True)
    assert len(check.issues) == 1
    assert check.issues[0].message.startswith("1001 existing rows")


def test_target_symlinks_are_not_followed(source, target, tmp_path):
    path, _ = target
    link = tmp_path / "link.sqlite"
    link.symlink_to(path)
    with pytest.raises(MigrationError, match="non-symlink"):
        validate_target(
            prepare(source), database_url=f"sqlite:///{link}", target_stopped=True
        )


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_unclean_sqlite_target_is_refused_without_recovery(source, target, suffix):
    path, url = target
    sidecar = path.with_name(path.name + suffix)
    sidecar.write_bytes(b"private-journal")
    with pytest.raises(MigrationError, match="sidecars"):
        validate_target(prepare(source), database_url=url, target_stopped=True)
    assert sidecar.read_bytes() == b"private-journal"


def test_database_connection_really_rejects_writes(target):
    path, url = target
    before = path.read_bytes()
    with pytest.raises(MigrationError):
        with _read_only_target(url) as conn:
            conn.execute(text("DELETE FROM users"))
    assert path.read_bytes() == before


def test_changed_target_invalidates_readonly_check(target):
    path, url = target
    with pytest.raises(MigrationError, match="changed"):
        with _read_only_target(url) as conn:
            assert conn.execute(text("SELECT count(*) FROM users")).scalar() == 1
            mutate(url, "UPDATE users SET status='inactive'")


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///:memory:",
        "sqlite:///relative.sqlite",
        "sqlite:////tmp/no.sqlite?mode=rwc",
        "mysql://private-user:private-password@localhost/db",
        "invalid-private-url",
        "postgresql:///database",
        "postgresql://localhost/database",
    ],
)
def test_unsupported_or_ambiguous_connection_urls_fail_safely(source, url):
    with pytest.raises(MigrationError) as error:
        validate_target(prepare(source), database_url=url, target_stopped=True)
    assert "private" not in str(error.value)


def test_target_ack_and_complete_plan_are_mandatory(source, target):
    _, url = target
    plan = prepare(source)
    with pytest.raises(MigrationError, match="stopped"):
        validate_target(plan, database_url=url)
    with pytest.raises(MigrationError, match="conversion"):
        validate_target(
            replace(plan, issues=(PlanIssue("blocked", "source"),)),
            database_url=url,
            target_stopped=True,
        )
    with pytest.raises(MigrationError, match="identities"):
        validate_target(
            replace(plan, identities=()), database_url=url, target_stopped=True
        )
