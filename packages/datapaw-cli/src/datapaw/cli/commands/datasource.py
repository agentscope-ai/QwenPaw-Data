"""Context Manager datasource discovery commands."""

from __future__ import annotations

import argparse
import json
from typing import Any

from datapaw.host.core.cm_client import CMDatasource, ContextManagerClient

MASKED_SECRET = "******"
SENSITIVE_CONFIG_FIELDS = frozenset(
    {
        "password",
        "access_key_id",
        "access_key_secret",
        "sts_token",
    },
)


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "datasource",
        help="Inspect datasources configured in DataBridge",
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


def _mask_config(config: dict[str, Any] | None) -> dict[str, Any] | None:
    if config is None:
        return None
    return {
        key: MASKED_SECRET
        if key in SENSITIVE_CONFIG_FIELDS and value not in (None, "")
        else value
        for key, value in config.items()
    }


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
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


__all__ = ["handle_list", "register"]
