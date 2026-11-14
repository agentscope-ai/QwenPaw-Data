"""Explicit offline inspection, backup and preparation, not SQL import."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "migration",
        help="Inspect, back up or plan legacy Host JSON migration (no SQL import)",
    )
    actions = parser.add_subparsers(dest="migration_action", required=True)
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
        help="Disjoint target home; never created or modified",
    )
    plan.add_argument("--identity-map", type=Path, required=True)
    plan.add_argument("--source-stopped", action="store_true", required=True)
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


def handle_plan(args: argparse.Namespace) -> int:
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
    plan = prepare_migration(
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
    print(json.dumps(plan.report(), ensure_ascii=False, indent=2))
    return 2 if plan.issues else 0
