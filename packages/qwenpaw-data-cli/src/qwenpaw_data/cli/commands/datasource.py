"""Context Manager datasource discovery and management commands."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from qwenpaw_data.host.core.cm_client import CMDatasource, ContextManagerClient
from qwenpaw_data.host.core.semantic_config_client import (
    SEMANTIC_CONFIG_PREFIX,
    SemanticConfigClient,
)

from qwenpaw_data.cli.util import (
    confirm_deletion,
    load_json_object,
    parse_json_object,
    print_json,
)

MASKED_SECRET = "******"
SENSITIVE_CONFIG_FIELDS = frozenset(
    {
        "password",
        "access_key_id",
        "access_key_secret",
        "sts_token",
    },
)

_DATASOURCE_PATH = f"{SEMANTIC_CONFIG_PREFIX}/datasource"


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "datasource",
        help="Manage datasources configured in DataBridge",
    )
    datasource_subparsers = parser.add_subparsers(
        dest="datasource_command",
        required=True,
    )

    list_parser = datasource_subparsers.add_parser(
        "list",
        help="List all DataBridge datasources",
    )
    list_parser.set_defaults(handler=handle_list)

    get_parser = datasource_subparsers.add_parser(
        "get",
        help="Show one datasource",
    )
    get_parser.add_argument("datasource_id", help="Datasource id")
    get_parser.add_argument(
        "--show-config",
        action="store_true",
        help="Include the connection config with sensitive fields masked",
    )
    get_parser.set_defaults(handler=handle_get)

    create_parser = datasource_subparsers.add_parser(
        "create",
        help="Create a datasource (requires a credentials:manage API key)",
    )
    create_parser.add_argument("--name", required=True, help="Datasource name")
    create_parser.add_argument(
        "--type",
        required=True,
        dest="datasource_type",
        help="Datasource type, e.g. postgresql / mysql / odps",
    )
    _add_config_args(create_parser, required=True)
    create_parser.add_argument(
        "--test",
        action="store_true",
        help="Test the connection first and abort the create on failure",
    )
    create_parser.set_defaults(handler=handle_create)

    update_parser = datasource_subparsers.add_parser(
        "update",
        help="Update a datasource; a provided config replaces the stored one",
    )
    update_parser.add_argument("datasource_id", help="Datasource id")
    update_parser.add_argument("--name", help="New datasource name")
    update_parser.add_argument(
        "--type",
        dest="datasource_type",
        help="New datasource type",
    )
    _add_config_args(update_parser, required=False)
    update_parser.set_defaults(handler=handle_update)

    delete_parser = datasource_subparsers.add_parser(
        "delete",
        help="Delete a datasource",
    )
    delete_parser.add_argument("datasource_id", help="Datasource id")
    delete_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation",
    )
    delete_parser.set_defaults(handler=handle_delete)

    test_parser = datasource_subparsers.add_parser(
        "test",
        help="Test connectivity of a saved datasource or an ad-hoc config",
    )
    test_parser.add_argument(
        "datasource_id",
        nargs="?",
        help="Saved datasource id (omit when testing an ad-hoc config)",
    )
    test_parser.add_argument(
        "--type",
        dest="datasource_type",
        help="Datasource type for an ad-hoc connection test",
    )
    _add_config_args(test_parser, required=False)
    test_parser.set_defaults(handler=handle_test)


def _add_config_args(parser: argparse.ArgumentParser, *, required: bool) -> None:
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument(
        "--config-file",
        type=Path,
        help="Path to a JSON file with the connection config",
    )
    group.add_argument(
        "--config",
        dest="config_inline",
        help="Inline JSON object with the connection config",
    )


def _load_config(args: argparse.Namespace) -> dict[str, Any] | None:
    if getattr(args, "config_file", None) is not None:
        return load_json_object(args.config_file)
    if getattr(args, "config_inline", None) is not None:
        return parse_json_object(args.config_inline, flag="--config")
    return None


def _mask_config(config: dict[str, Any] | None) -> dict[str, Any] | None:
    if config is None:
        return None
    return {
        key: MASKED_SECRET
        if key in SENSITIVE_CONFIG_FIELDS and value not in (None, "")
        else value
        for key, value in config.items()
    }


def _masked_record(record: dict[str, Any], *, include_config: bool) -> dict[str, Any]:
    payload = {
        "datasource_id": record.get("datasource_id"),
        "datasource_name": record.get("datasource_name"),
        "datasource_type": record.get("datasource_type"),
    }
    if include_config:
        config = record.get("config")
        payload["config"] = _mask_config(config if isinstance(config, dict) else None)
    return payload


def _output_item(item: CMDatasource) -> dict[str, Any]:
    return {
        "datasource_id": item.datasource_id,
        "datasource_name": item.datasource_name,
        "datasource_type": item.datasource_type,
        "config": _mask_config(item.config),
    }


def handle_list(_: argparse.Namespace) -> int:
    result = ContextManagerClient().list_datasources()
    payload = {
        "items": [_output_item(item) for item in result.items],
        "total": result.total,
    }
    print_json(payload)
    return 0


def handle_get(args: argparse.Namespace) -> int:
    record = SemanticConfigClient().get(
        f"{_DATASOURCE_PATH}/{args.datasource_id}",
    )
    print_json(_masked_record(record, include_config=args.show_config))
    return 0


def handle_create(args: argparse.Namespace) -> int:
    config = _load_config(args)
    client = SemanticConfigClient()
    if args.test:
        result = client.post(
            f"{_DATASOURCE_PATH}/test-connection",
            json={"datasource_type": args.datasource_type, "config": config},
        )
        if not result.get("success"):
            raise ValueError(
                f"connection test failed, datasource not created: {result.get('message')}",
            )
    record = client.post(
        _DATASOURCE_PATH,
        json={
            "datasource_name": args.name,
            "datasource_type": args.datasource_type,
            "config": config,
        },
    )
    print_json(_masked_record(record, include_config=True))
    return 0


def handle_update(args: argparse.Namespace) -> int:
    config = _load_config(args)
    payload: dict[str, Any] = {}
    if args.name is not None:
        payload["datasource_name"] = args.name
    if args.datasource_type is not None:
        payload["datasource_type"] = args.datasource_type
    if config is not None:
        payload["config"] = config
    if not payload:
        raise ValueError("nothing to update: pass --name, --type, or a config")
    record = SemanticConfigClient().put(
        f"{_DATASOURCE_PATH}/{args.datasource_id}",
        json=payload,
    )
    print_json(_masked_record(record, include_config=True))
    return 0


def handle_delete(args: argparse.Namespace) -> int:
    if not confirm_deletion(f"datasource {args.datasource_id}", assume_yes=args.yes):
        print("aborted")
        return 1
    result = SemanticConfigClient().delete(
        f"{_DATASOURCE_PATH}/{args.datasource_id}",
    )
    print_json(result if result else {"deleted": args.datasource_id})
    return 0


def handle_test(args: argparse.Namespace) -> int:
    config = _load_config(args)
    ad_hoc = args.datasource_type is not None or config is not None
    if args.datasource_id and ad_hoc:
        raise ValueError("pass either a datasource id or --type with a config, not both")
    client = SemanticConfigClient()
    if args.datasource_id:
        result = client.post(
            f"{_DATASOURCE_PATH}/{args.datasource_id}/test-connection",
        )
    elif args.datasource_type is not None and config is not None:
        result = client.post(
            f"{_DATASOURCE_PATH}/test-connection",
            json={"datasource_type": args.datasource_type, "config": config},
        )
    else:
        raise ValueError("pass a datasource id, or --type together with a config")
    print_json(result)
    return 0 if result.get("success") else 1


__all__ = [
    "handle_create",
    "handle_delete",
    "handle_get",
    "handle_list",
    "handle_test",
    "handle_update",
    "register",
]
