"""Explicit offline legacy-data inspection and backup, not SQL import."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "migration", help="Inspect or back up legacy Host JSON (no SQL import)"
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
    for command in (inspect, backup):
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
