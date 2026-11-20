"""Explicit offline inspection, saved-plan import/recovery and owned rollback."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "migration",
        help="Explicit offline legacy migration: inspect, prepare, apply/recover or rollback",
    )
    actions = parser.add_subparsers(dest="migration_action", required=True)
    discover = actions.add_parser(
        "discover", help="Locate possible older local SQLite stores without opening them"
    )
    discover.add_argument("--source-home", type=Path, help="Defaults to the OSS runtime home")
    discover.set_defaults(handler=handle_discover)
    inspect = actions.add_parser(
        "inspect",
        help="Read-only source inventory and identity/reference checks",
        epilog='Mapping format: {"version":1,"users":{"old-id":{"user_id":"new-id","tenant_id":"tenant","workspace_id":"workspace"}}}. No implicit user/tenant defaults. Passing these checks is not SQL import acceptance.',
    )
    inspect.add_argument(
        "--source-home",
        type=Path,
        required=True,
        help="Explicit old QWENPAW_DATA_HOME; no default",
    )
    inspect.add_argument(
        "--identity-map",
        type=Path,
        help="Version 1 JSON mapping old users to explicit target identities",
    )
    inspect.set_defaults(handler=handle_inspect)
    backup = actions.add_parser(
        "backup", help="Copy and verify all legacy host files; never overwrite"
    )
    backup.add_argument("--source-home", type=Path, required=True)
    backup.add_argument(
        "--destination",
        type=Path,
        required=True,
        help="New sibling/outside directory; parent must exist",
    )
    backup.add_argument(
        "--source-stopped",
        action="store_true",
        required=True,
        help="Confirm all applications writing this source have been stopped",
    )
    backup.set_defaults(handler=handle_backup)
    plan = actions.add_parser(
        "plan",
        help="Read-only conversion and scoped-file preparation; not an import command",
    )
    plan.add_argument("--source-home", type=Path, required=True)
    plan.add_argument(
        "--backup",
        type=Path,
        required=True,
        help="Existing verified backup from migration backup",
    )
    plan.add_argument(
        "--target-home",
        type=Path,
        required=True,
        help="Explicit disjoint target home; plan never creates or modifies it",
    )
    plan.add_argument("--identity-map", type=Path, required=True)
    plan.add_argument("--source-stopped", action="store_true", required=True)
    plan.add_argument(
        "--target-db-env",
        help="Environment variable NAME holding an initialized target DB URL; optional read-only check for plan, required metadata write for prepare",
    )
    plan.add_argument(
        "--target-stopped",
        action="store_true",
        help="Confirm target writers are stopped; required with --target-db-env",
    )
    plan.add_argument(
        "--source-key-env",
        help="Explicit environment variable NAME holding the old hex master key; omit only for plaintext credentials",
    )
    plan.add_argument(
        "--target-key-env",
        required=True,
        help="Explicit environment variable NAME holding the intended target 32-byte hex master key; never pass the key itself",
    )
    plan.add_argument(
        "--context-trigger-ratio",
        type=float,
        required=True,
        help="Explicit target runtime context trigger default",
    )
    plan.add_argument(
        "--context-reserve-ratio",
        type=float,
        required=True,
        help="Explicit target runtime context reserve default",
    )
    plan.add_argument(
        "--model-context-sizes",
        type=Path,
        help='Version 1 JSON: {"version":1,"users":{"old-user":{"provider/model":12345}}}',
    )
    plan.add_argument(
        "--disable-cron",
        action="store_true",
        help="Consent to propose imported cron as disabled; never execute schedules",
    )
    plan.set_defaults(handler=handle_plan)
    key_setup = actions.add_parser(
        "provision-key",
        help="Explicitly publish a private local target key, without replacing an existing key or writing SQL",
    )
    key_setup.add_argument("--target-home", type=Path, required=True)
    key_setup.add_argument("--target-key-env", required=True)
    key_setup.add_argument("--target-db-env", required=True)
    key_setup.add_argument("--target-stopped", action="store_true", required=True)
    key_setup.set_defaults(handler=handle_provision_key)
    for command in (inspect, backup, plan):
        command.add_argument("--max-files", type=int, default=100_000)
        command.add_argument(
            "--max-bytes",
            type=int,
            default=10 * 1024**3,
            help="Maximum source bytes (default: 10 GiB)",
        )
        command.add_argument(
            "--max-metadata-bytes",
            type=int,
            default=64 * 1024**2,
            help="Maximum individual metadata document/event log (default: 64 MiB)",
        )
    # Reuse conversion flags without changing the read-only plan command.
    prepare = actions.add_parser(
        "prepare",
        parents=[plan],
        add_help=False,
        help="Save an encrypted plan and target metadata token; no business-row import",
    )
    prepare.add_argument(
        "--recovery-plan",
        type=Path,
        required=True,
        help="New absolute private plan path; existing plans are never replaced",
    )
    prepare.add_argument(
        "--run-id",
        required=True,
        help="Explicit unique 32-character lowercase hex run ID",
    )
    prepare.add_argument(
        "--deployment-config-confirmed",
        action="store_true",
        required=True,
        help="Confirm all target services will use this home, database and key",
    )
    prepare.set_defaults(handler=handle_prepare)
    for action in ("apply", "recover", "rollback", "check-rollback"):
        command = actions.add_parser(
            action,
            help={
                "apply": "Apply exactly one saved plan; retain backup and recovery evidence",
                "recover": "Resume the same saved import, never replan or overwrite changed data",
                "rollback": "Undo unchanged owned imported rows/files; retain backup, staging and directories",
                "check-rollback": "Read-only known-dependency check; not deletion authorization",
            }[action],
        )
        command.add_argument("--recovery-plan", type=Path, required=True)
        command.add_argument("--target-home", type=Path, required=True)
        command.add_argument("--target-db-env", required=True)
        command.add_argument("--target-key-env", required=True)
        command.add_argument("--target-stopped", action="store_true", required=True)
        if action != "check-rollback":
            command.add_argument(
                "--confirm-run-id",
                required=True,
                help="Must exactly match the authenticated plan's run ID",
            )
        if action in {"apply", "recover"}:
            command.add_argument(
                "--deployment-config-confirmed",
                action="store_true",
                required=True,
                help="Confirm all target services use the selected home, database and key",
            )
        command.set_defaults(handler=handle_saved_plan)


def handle_discover(args: argparse.Namespace) -> int:
    from qwenpaw_data_auth.config import runtime_home
    from qwenpaw_data.host.core.upgrades import discover_local_stores

    print(json.dumps(discover_local_stores(args.source_home or runtime_home()), indent=2))
    return 0


def _limits(args):
    from qwenpaw_data.host.core.migration.legacy_json import Limits

    return Limits(
        files=args.max_files,
        bytes=args.max_bytes,
        metadata_bytes=args.max_metadata_bytes,
    )


def handle_inspect(args: argparse.Namespace) -> int:
    from qwenpaw_data.host.core.migration.legacy_json import (
        inspect_source,
        load_identity_mapping,
    )

    mapping = load_identity_mapping(args.identity_map) if args.identity_map else None
    inspection = inspect_source(args.source_home, users=mapping, limits=_limits(args))
    print(json.dumps(inspection.report(), ensure_ascii=False, indent=2))
    return 2 if inspection.findings else 0


def handle_backup(args: argparse.Namespace) -> int:
    from qwenpaw_data.host.core.migration.legacy_json import create_backup, scan_source

    snapshot = scan_source(args.source_home, limits=_limits(args))
    status = create_backup(
        snapshot, args.destination, source_stopped=args.source_stopped
    )
    print(
        json.dumps(
            {
                "stage": "backup_only",
                "sql_written": False,
                "status": status,
                "source_digest": snapshot.digest,
                "files": len(snapshot.files),
                "destination": str(args.destination.resolve()),
                "warning": "Backup may contain credentials and private data. Keep it private; this is not SQL import.",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _master_key(variable: str, *, legacy: bool) -> bytes:
    import os
    import re
    from qwenpaw_data.host.core.migration.legacy_json import MigrationError

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
        raise MigrationError(
            "Master-key arguments must be environment variable names, not secrets"
        )
    value = os.environ.get(variable)
    if not value or len(value) > 4096:
        raise MigrationError(
            "Explicit master-key variable is missing, empty or too large"
        )
    try:
        key = bytes.fromhex(value.strip())
    except ValueError:
        raise MigrationError(
            "Explicit master-key variable must contain hexadecimal bytes"
        ) from None
    if (legacy and len(key) < 32) or (not legacy and len(key) != 32):
        raise MigrationError(
            "Legacy master key needs at least 32 bytes; target key needs exactly 32 bytes"
        )
    return key


def _model_sizes(path: Path | None):
    from qwenpaw_data.host.core.migration.legacy_json import (
        MigrationError,
        _json,
        _read_file,
        _signature,
    )

    if path is None:
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024**2:
        raise MigrationError(
            "Model size mapping must be a regular JSON file under 1 MiB"
        )
    with _read_file(
        path.parent.resolve(), path.name, _signature(path.lstat())
    ) as stream:
        data = stream.read(1024**2 + 1)
    if len(data) > 1024**2:
        raise MigrationError("Model size mapping exceeds 1 MiB")
    document = _json(data, "model context-size mapping")
    if (
        not isinstance(document, dict)
        or set(document) != {"version", "users"}
        or type(document["version"]) is not int
        or document["version"] != 1
        or not isinstance(document["users"], dict)
    ):
        raise MigrationError("Model size mapping requires version=1 and a users object")
    return document["users"]


def _prepare_conversion(args: argparse.Namespace):
    from datetime import datetime, timezone
    from qwenpaw_data.host.core.migration.credentials import CredentialRewriter
    from qwenpaw_data.host.core.migration.legacy_json import load_identity_mapping
    from qwenpaw_data.host.core.migration.plan import prepare_migration
    from qwenpaw_data.host.core.providers import provider_registry

    rewriter = CredentialRewriter(
        source_key=_master_key(args.source_key_env, legacy=True)
        if args.source_key_env
        else None,
        target_key=_master_key(args.target_key_env, legacy=False),
    )
    return prepare_migration(
        args.source_home,
        backup=args.backup,
        target_home=args.target_home,
        users=load_identity_mapping(args.identity_map),
        credentials=rewriter,
        registry=provider_registry,
        prepared_at=datetime.now(timezone.utc),
        target_context_trigger_ratio=args.context_trigger_ratio,
        target_context_reserve_ratio=args.context_reserve_ratio,
        model_context_sizes=_model_sizes(args.model_context_sizes),
        disable_cron=args.disable_cron,
        source_stopped=args.source_stopped,
        limits=_limits(args),
    )


def handle_plan(args: argparse.Namespace) -> int:
    plan = _prepare_conversion(args)
    report = plan.report()
    target_issues = ()
    if args.target_db_env or args.target_stopped:
        from qwenpaw_data.host.core.migration.legacy_json import MigrationError
        from qwenpaw_data.host.core.migration.target import validate_target

        if not args.target_db_env or not args.target_stopped:
            raise MigrationError(
                "Target checks require both --target-db-env and --target-stopped"
            )
        database_url = _database_url(args.target_db_env)
        check = validate_target(plan, database_url=database_url, target_stopped=True)
        report["target_validation"] = check.report()
        report["target_database_checked"] = True
        report["target_identity_membership_checked"] = True
        target_issues = check.issues
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if plan.issues or target_issues else 0


def _database_url(variable: str) -> str:
    import os
    import re
    from qwenpaw_data.host.core.migration.legacy_json import MigrationError

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
        raise MigrationError(
            "Target database argument must be an environment variable name, not a connection URL"
        )
    value = os.environ.get(variable)
    if not value or len(value) > 16384:
        raise MigrationError(
            "Explicit target database variable is missing, empty or too large"
        )
    return value


def handle_provision_key(args: argparse.Namespace) -> int:
    from qwenpaw_data.host.core.migration.key_setup import provision_local_key

    result = provision_local_key(
        args.target_home,
        key=_master_key(args.target_key_env, legacy=False),
        database_url=_database_url(args.target_db_env),
        target_stopped=args.target_stopped,
    )
    print(json.dumps(result.report(), ensure_ascii=False, indent=2))
    return 0


def handle_prepare(args: argparse.Namespace) -> int:
    import os
    from qwenpaw_data.host.core.migration.binding import ensure_target_anchor
    from qwenpaw_data.host.core.migration.bundle import (
        _parent,
        save_recovery_plan,
        load_recovery_plan,
    )
    from qwenpaw_data.host.core.migration.key_setup import (
        verify_selected_runtime_key,
        check_database_credentials,
    )
    from qwenpaw_data.host.core.migration.legacy_json import MigrationError
    from qwenpaw_data.host.core.migration.locking import migration_lock
    from qwenpaw_data.host.core.migration.sql_import import _run_id
    from qwenpaw_data.host.core.migration.target import (
        validate_target,
        _read_only_target,
    )

    if not args.target_db_env or args.target_stopped is not True:
        raise MigrationError("prepare requires --target-db-env and --target-stopped")
    _run_id(args.run_id)
    key = _master_key(args.target_key_env, legacy=False)
    url = _database_url(args.target_db_env)
    key_source = verify_selected_runtime_key(args.target_home, key=key)
    plan = _prepare_conversion(args)
    if plan.issues:
        print(json.dumps(plan.report(), ensure_ascii=False, indent=2))
        return 2
    # Reject unsafe output/overwrite before any database metadata write.
    with _parent(args.recovery_plan) as (parent, name, resolved):
        if any(
            resolved.is_relative_to(p.resolve())
            for p in (args.source_home, args.backup, args.target_home)
        ):
            raise MigrationError(
                "CLI recovery plans must be outside source, backup and target home"
            )
        try:
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise MigrationError(
                "Recovery plan already exists; use apply/recover with the original plan, never reprepare"
            )
    with migration_lock(plan.target_home, target_stopped=True):
        checked = validate_target(plan, database_url=url, target_stopped=True)
        if checked.issues:
            print(json.dumps(checked.report(), ensure_ascii=False, indent=2))
            return 2
        with _read_only_target(url) as conn:
            check_database_credentials(conn, key)
        anchor = ensure_target_anchor(url, target_stopped=True)
        status = save_recovery_plan(
            args.recovery_plan,
            plan,
            backup=args.backup,
            database_url=url,
            run_id=args.run_id,
            target_anchor=anchor,
            key=key,
            source_stopped=args.source_stopped,
            target_stopped=True,
        )
        saved = load_recovery_plan(
            args.recovery_plan, database_url=url, target_home=plan.target_home, key=key
        )
    print(
        json.dumps(
            {
                "stage": "saved_recovery_plan",
                "status": status,
                "run_id": saved.run_id,
                "plan_digest": saved.digest,
                "recovery_plan": str(args.recovery_plan),
                "business_rows_written": False,
                "target_metadata_token_recorded": True,
                "key_source_checked": key_source,
                "remote_runtime_configuration_verified": False,
                "warning": "Keep this exact private plan, key and independent backup. Retry interrupted imports with recover; never reprepare. No automatic staging cleanup.",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def handle_saved_plan(args: argparse.Namespace) -> int:
    from qwenpaw_data.host.core.migration.bundle import load_recovery_plan
    from qwenpaw_data.host.core.migration.coordinator import apply_saved_plan
    from qwenpaw_data.host.core.migration.key_setup import verify_selected_runtime_key
    from qwenpaw_data.host.core.migration.legacy_json import MigrationError
    from qwenpaw_data.host.core.migration.rollback import rollback_saved_plan
    from qwenpaw_data.host.core.migration.rollback_check import inspect_saved_rollback

    key = _master_key(args.target_key_env, legacy=False)
    url = _database_url(args.target_db_env)
    if args.target_home.is_symlink() or not args.target_home.is_absolute():
        raise MigrationError(
            "Target home must be explicit, absolute and not a final symbolic link"
        )
    recovery = load_recovery_plan(
        args.recovery_plan, database_url=url, target_home=args.target_home, key=key
    )
    if (
        args.migration_action != "check-rollback"
        and args.confirm_run_id != recovery.run_id
    ):
        raise MigrationError("--confirm-run-id must match the authenticated saved plan")
    key_source = None
    if args.migration_action in {"apply", "recover"}:
        key_source = verify_selected_runtime_key(args.target_home, key=key)
        operation = apply_saved_plan
    else:
        operation = (
            rollback_saved_plan
            if args.migration_action == "rollback"
            else inspect_saved_rollback
        )
    result = operation(
        args.recovery_plan,
        database_url=url,
        target_home=args.target_home,
        key=key,
        target_stopped=args.target_stopped,
        **(
            {"expected_run_id": args.confirm_run_id}
            if args.migration_action != "check-rollback"
            else {}
        ),
    )
    report = result.report()
    report["cli_action"] = args.migration_action
    report["key_source_checked"] = key_source
    report["remote_runtime_configuration_verified"] = False
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if getattr(result, "findings", ()) else 0
