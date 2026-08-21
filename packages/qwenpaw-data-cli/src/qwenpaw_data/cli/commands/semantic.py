"""Semantic-layer configuration commands: CRUD, Excel import, and weave."""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from qwenpaw_data.host.core.semantic_config_client import (
    SEMANTIC_CONFIG_PREFIX,
    SemanticConfigClient,
)

from qwenpaw_data.cli.util import (
    confirm_deletion,
    load_json_object,
    print_json,
)

_WEAVE_PATH = f"{SEMANTIC_CONFIG_PREFIX}/weave-task"
# DataBridge reports weave states in upper case (SUCCESS/FAILED/KILLED);
# compare case-insensitively to stay tolerant of both spellings.
_WEAVE_TERMINAL_STATES = frozenset({"success", "failed", "killed"})
_WEAVE_POLL_SECONDS = 2.0


def _weave_status(record: dict[str, Any]) -> str:
    return str(record.get("status") or "").lower()


@dataclass(frozen=True)
class Field:
    """One first-class CLI flag mapped to an API payload/query key."""

    flag: str
    key: str
    value_type: Callable[[str], Any] = str
    is_bool: bool = False
    help: str = ""

    @property
    def dest(self) -> str:
        return f"field_{self.key}"


@dataclass(frozen=True)
class Resource:
    """Declarative description of one semantic-config CRUD resource."""

    name: str
    path: str
    help: str
    list_filters: tuple[Field, ...]
    create_fields: tuple[Field, ...]
    update_fields: tuple[Field, ...]
    dataset_batch_delete: bool = False

    @property
    def api_path(self) -> str:
        return f"{SEMANTIC_CONFIG_PREFIX}/{self.path}"


def _f(flag: str, key: str, **kwargs: Any) -> Field:
    return Field(flag=flag, key=key, **kwargs)


RESOURCES: tuple[Resource, ...] = (
    Resource(
        name="domain",
        path="biz-domain",
        help="Business domains",
        list_filters=(
            _f("--datasource-id", "datasource_id"),
            _f("--name", "domain_name"),
        ),
        create_fields=(
            _f("--datasource-id", "datasource_id"),
            _f("--name", "domain_name"),
            _f("--display-name", "display_name"),
            _f("--description", "description"),
            _f("--aliases", "aliases"),
        ),
        update_fields=(
            _f("--name", "domain_name"),
            _f("--display-name", "display_name"),
            _f("--description", "description"),
            _f("--aliases", "aliases"),
        ),
    ),
    Resource(
        name="dataset",
        path="dataset-meta",
        help="Datasets",
        list_filters=(
            _f("--datasource-id", "datasource_id"),
            _f("--domain-id", "domain_id", value_type=int),
            _f("--name", "dataset_name"),
            _f("--type", "dataset_type"),
        ),
        create_fields=(
            _f("--datasource-id", "datasource_id"),
            _f("--domain-id", "domain_id", value_type=int),
            _f("--name", "dataset_name"),
            _f("--comment", "dataset_comment"),
            _f("--type", "dataset_type"),
            _f("--sql", "sql_content"),
            _f("--parents", "parents"),
        ),
        update_fields=(
            _f("--name", "dataset_name"),
            _f("--comment", "dataset_comment"),
            _f("--type", "dataset_type"),
            _f("--sql", "sql_content"),
            _f("--parents", "parents"),
        ),
    ),
    Resource(
        name="column",
        path="dataset-column-meta",
        help="Dataset columns",
        list_filters=(
            _f("--datasource-id", "datasource_id"),
            _f("--domain-id", "domain_id", value_type=int),
            _f("--dataset-id", "dataset_id", value_type=int),
        ),
        create_fields=(
            _f("--dataset-id", "dataset_id", value_type=int),
            _f("--name", "column_name"),
            _f("--comment", "column_comment"),
            _f("--data-type", "data_type"),
        ),
        update_fields=(
            _f("--name", "column_name"),
            _f("--comment", "column_comment"),
            _f("--data-type", "data_type"),
        ),
    ),
    Resource(
        name="dimension",
        path="dimension",
        help="Dimensions",
        list_filters=(
            _f("--datasource-id", "datasource_id"),
            _f("--domain-id", "domain_id", value_type=int),
            _f("--name", "dimension_name"),
        ),
        create_fields=(
            _f("--datasource-id", "datasource_id"),
            _f("--domain-id", "domain_id", value_type=int),
            _f("--name", "dimension_name"),
            _f("--description", "description"),
            _f("--parent-name", "parent_name"),
            _f("--synonyms", "synonyms"),
            _f("--enums", "enums"),
        ),
        update_fields=(
            _f("--name", "dimension_name"),
            _f("--description", "description"),
            _f("--parent-name", "parent_name"),
            _f("--synonyms", "synonyms"),
            _f("--enums", "enums"),
        ),
    ),
    Resource(
        name="binding",
        path="dataset-dimension",
        help="Dataset-dimension bindings",
        list_filters=(
            _f("--datasource-id", "datasource_id"),
            _f("--domain-id", "domain_id", value_type=int),
            _f("--dataset-id", "dataset_id", value_type=int),
            _f("--dataset-name", "dataset_name"),
            _f("--dimension-name", "dimension_name"),
        ),
        create_fields=(
            _f("--dataset-id", "dataset_id", value_type=int),
        ),
        update_fields=(),
        dataset_batch_delete=True,
    ),
    Resource(
        name="metric",
        path="metric-lib",
        help="Metrics",
        list_filters=(
            _f("--datasource-id", "datasource_id"),
            _f("--domain-id", "domain_id", value_type=int),
            _f("--name", "metric_name"),
        ),
        create_fields=(
            _f("--datasource-id", "datasource_id"),
            _f("--domain-id", "domain_id", value_type=int),
            _f("--name", "metric_name"),
            _f("--description", "description"),
            _f("--unit", "unit"),
            _f("--synonyms", "synonyms"),
            _f("--tags", "tags"),
            _f("--polaris", "is_polaris", is_bool=True),
        ),
        update_fields=(
            _f("--name", "metric_name"),
            _f("--description", "description"),
            _f("--unit", "unit"),
            _f("--synonyms", "synonyms"),
            _f("--tags", "tags"),
        ),
    ),
    Resource(
        name="formula",
        path="metric-formula-lib",
        help="Metric formulas",
        list_filters=(
            _f("--datasource-id", "datasource_id"),
            _f("--domain-id", "domain_id", value_type=int),
            _f("--metric-id", "metric_id", value_type=int),
            _f("--dataset-id", "dataset_id", value_type=int),
        ),
        create_fields=(
            _f("--metric-id", "metric_id", value_type=int),
            _f("--dataset-id", "dataset_id", value_type=int),
            _f("--formula", "formula"),
        ),
        update_fields=(
            _f("--formula", "formula"),
        ),
        dataset_batch_delete=True,
    ),
)


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "semantic",
        help="Manage the DataBridge semantic configuration layer",
    )
    semantic_subparsers = parser.add_subparsers(
        dest="semantic_command",
        required=True,
    )

    for resource in RESOURCES:
        _register_resource(semantic_subparsers, resource)

    import_parser = semantic_subparsers.add_parser(
        "import",
        help="Import a semantic configuration workbook (.xlsx)",
    )
    import_parser.add_argument(
        "--file",
        type=Path,
        required=True,
        help="Path to the semantic configuration .xlsx workbook",
    )
    import_parser.set_defaults(handler=handle_import)

    _register_weave(semantic_subparsers)


def _register_resource(
    subparsers: argparse._SubParsersAction,
    resource: Resource,
) -> None:
    parser = subparsers.add_parser(resource.name, help=resource.help)
    actions = parser.add_subparsers(
        dest=f"{resource.name}_command",
        required=True,
    )

    list_parser = actions.add_parser("list", help=f"List {resource.help.lower()}")
    for filter_field in resource.list_filters:
        list_parser.add_argument(
            filter_field.flag,
            dest=filter_field.dest,
            type=filter_field.value_type,
        )
    list_parser.add_argument("--page", type=int, default=1)
    list_parser.add_argument("--size", type=int, default=20)
    list_parser.add_argument(
        "--all",
        action="store_true",
        help="Fetch every page",
    )
    list_parser.set_defaults(handler=handle_resource_list, resource=resource)

    get_parser = actions.add_parser("get", help="Show one record")
    get_parser.add_argument("record_id", type=int, help="Record id")
    get_parser.set_defaults(handler=handle_resource_get, resource=resource)

    create_parser = actions.add_parser(
        "create",
        help="Create a record (fields via flags, or --file with a JSON object)",
    )
    _add_payload_args(create_parser, resource.create_fields)
    create_parser.set_defaults(handler=handle_resource_create, resource=resource)

    update_parser = actions.add_parser(
        "update",
        help="Update a record (fields via flags, or --file with a JSON object)",
    )
    update_parser.add_argument("record_id", type=int, help="Record id")
    _add_payload_args(update_parser, resource.update_fields)
    update_parser.set_defaults(handler=handle_resource_update, resource=resource)

    delete_parser = actions.add_parser("delete", help="Delete a record")
    if resource.dataset_batch_delete:
        delete_parser.add_argument(
            "record_id",
            type=int,
            nargs="?",
            help="Record id (omit when using --dataset-id)",
        )
        delete_parser.add_argument(
            "--dataset-id",
            type=int,
            help=f"Delete every {resource.name} of one dataset",
        )
    else:
        delete_parser.add_argument("record_id", type=int, help="Record id")
    delete_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation",
    )
    delete_parser.set_defaults(handler=handle_resource_delete, resource=resource)


def _add_payload_args(
    parser: argparse.ArgumentParser,
    fields: tuple[Field, ...],
) -> None:
    parser.add_argument(
        "--file",
        type=Path,
        dest="payload_file",
        help="JSON object file with the full request body",
    )
    for payload_field in fields:
        if payload_field.is_bool:
            parser.add_argument(
                payload_field.flag,
                dest=payload_field.dest,
                action="store_true",
                default=None,
            )
        else:
            parser.add_argument(
                payload_field.flag,
                dest=payload_field.dest,
                type=payload_field.value_type,
            )


def _build_payload(
    args: argparse.Namespace,
    fields: tuple[Field, ...],
) -> dict[str, Any]:
    """Assemble the request body from --file XOR first-class flags."""
    flag_values = {
        payload_field.key: getattr(args, payload_field.dest)
        for payload_field in fields
        if getattr(args, payload_field.dest, None) is not None
    }
    payload_file = getattr(args, "payload_file", None)
    if payload_file is not None and flag_values:
        raise ValueError("pass either --file or field flags, not both")
    if payload_file is not None:
        return load_json_object(payload_file)
    if not flag_values:
        raise ValueError("nothing to send: pass field flags or --file")
    return flag_values


def _filter_params(args: argparse.Namespace, fields: tuple[Field, ...]) -> dict[str, Any]:
    return {
        filter_field.key: getattr(args, filter_field.dest)
        for filter_field in fields
        if getattr(args, filter_field.dest, None) is not None
    }


def handle_resource_list(args: argparse.Namespace) -> int:
    resource: Resource = args.resource
    client = SemanticConfigClient()
    params = _filter_params(args, resource.list_filters)
    if args.all:
        page = client.list_all(resource.api_path, params=params)
    else:
        page = client.list_page(
            resource.api_path,
            params=params,
            page=args.page,
            size=args.size,
        )
    print_json({"items": page["records"], "total": page["total"]})
    return 0


def handle_resource_get(args: argparse.Namespace) -> int:
    resource: Resource = args.resource
    record = SemanticConfigClient().get(f"{resource.api_path}/{args.record_id}")
    print_json(record)
    return 0


def handle_resource_create(args: argparse.Namespace) -> int:
    resource: Resource = args.resource
    payload = _build_payload(args, resource.create_fields)
    record = SemanticConfigClient().post(resource.api_path, json=payload)
    print_json(record)
    return 0


def handle_resource_update(args: argparse.Namespace) -> int:
    resource: Resource = args.resource
    payload = _build_payload(args, resource.update_fields)
    record = SemanticConfigClient().put(
        f"{resource.api_path}/{args.record_id}",
        json=payload,
    )
    print_json(record)
    return 0


def handle_resource_delete(args: argparse.Namespace) -> int:
    resource: Resource = args.resource
    dataset_id = getattr(args, "dataset_id", None)
    if dataset_id is not None and args.record_id is not None:
        raise ValueError("pass either a record id or --dataset-id, not both")
    if dataset_id is not None:
        subject = f"every {resource.name} of dataset {dataset_id}"
        path = f"{resource.api_path}/dataset/{dataset_id}"
    elif args.record_id is not None:
        subject = f"{resource.name} {args.record_id}"
        path = f"{resource.api_path}/{args.record_id}"
    else:
        raise ValueError("pass a record id or --dataset-id")
    if not confirm_deletion(subject, assume_yes=args.yes):
        print("aborted")
        return 1
    result = SemanticConfigClient().delete(path)
    print_json(result if result else {"deleted": subject})
    return 0


def handle_import(args: argparse.Namespace) -> int:
    path: Path = args.file.expanduser()
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"unable to read {path}: {exc}") from exc
    result = SemanticConfigClient().post(
        f"{SEMANTIC_CONFIG_PREFIX}/import/excel",
        files={
            "file": (
                path.name,
                content,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        },
    )
    print_json(result)
    return 0


def _register_weave(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "weave",
        help="Publish the semantic configuration into the graph store",
    )
    actions = parser.add_subparsers(dest="weave_command", required=True)

    submit_parser = actions.add_parser("submit", help="Submit a weave task")
    submit_parser.add_argument(
        "--datasource-id",
        required=True,
        help="Datasource whose semantic configuration should be woven",
    )
    submit_parser.add_argument(
        "--mode",
        default="FULL",
        help="Weave mode (default: FULL)",
    )
    submit_parser.add_argument("--name", help="Optional task name")
    submit_parser.add_argument(
        "--wait",
        action="store_true",
        help="Poll until the task reaches a terminal state",
    )
    submit_parser.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="Maximum seconds to wait with --wait (default: 600)",
    )
    submit_parser.set_defaults(handler=handle_weave_submit)

    list_parser = actions.add_parser("list", help="List weave tasks")
    list_parser.add_argument("--datasource-name", dest="datasource_name")
    list_parser.add_argument("--task-name", dest="task_name")
    list_parser.add_argument("--page", type=int, default=1)
    list_parser.add_argument("--size", type=int, default=20)
    list_parser.set_defaults(handler=handle_weave_list)

    kill_parser = actions.add_parser("kill", help="Kill a running weave task")
    kill_parser.add_argument("task_id", help="Weave task id")
    kill_parser.set_defaults(handler=handle_weave_kill)


def _find_weave_task(client: SemanticConfigClient, task_id: str) -> dict[str, Any] | None:
    page = client.list_page(_WEAVE_PATH, page=1, size=100)
    for record in page["records"]:
        if isinstance(record, dict) and record.get("task_id") == task_id:
            return record
    return None


def _wait_for_weave_task(
    client: SemanticConfigClient,
    task: dict[str, Any],
    *,
    timeout: float,
    poll_seconds: float = _WEAVE_POLL_SECONDS,
) -> tuple[dict[str, Any], bool]:
    """Poll until the task is terminal. Returns (last_record, finished)."""
    task_id = str(task.get("task_id") or "")
    last_status = _weave_status(task)
    print(f"weave task {task_id}: {last_status or 'submitted'}", file=sys.stderr)
    deadline = time.monotonic() + timeout
    current = task
    while time.monotonic() < deadline:
        status = _weave_status(current)
        if status != last_status:
            print(f"\nweave task {task_id}: {status}", file=sys.stderr)
            last_status = status
        if status in _WEAVE_TERMINAL_STATES:
            print("", file=sys.stderr)
            return current, True
        print(".", end="", file=sys.stderr, flush=True)
        time.sleep(poll_seconds)
        current = _find_weave_task(client, task_id) or current
    print("", file=sys.stderr)
    return current, False


def handle_weave_submit(args: argparse.Namespace) -> int:
    client = SemanticConfigClient()
    task = client.post(
        f"{_WEAVE_PATH}/submit",
        json={
            "datasource_id": args.datasource_id,
            "task_name": args.name,
            "weave_mode": args.mode,
        },
    )
    if not args.wait:
        print_json(task)
        return 0

    task, finished = _wait_for_weave_task(client, task, timeout=args.timeout)
    print_json(task)
    if not finished:
        task_id = task.get("task_id")
        raise ValueError(
            f"timed out waiting for weave task {task_id}; it keeps running — "
            f"inspect it with 'qwenpaw-data semantic weave list' or stop it with "
            f"'qwenpaw-data semantic weave kill {task_id}'",
        )
    return 0 if _weave_status(task) == "success" else 1


def handle_weave_list(args: argparse.Namespace) -> int:
    page = SemanticConfigClient().list_page(
        _WEAVE_PATH,
        params={
            "datasource_name": args.datasource_name,
            "task_name": args.task_name,
        },
        page=args.page,
        size=args.size,
    )
    print_json({"items": page["records"], "total": page["total"]})
    return 0


def handle_weave_kill(args: argparse.Namespace) -> int:
    result = SemanticConfigClient().post(f"{_WEAVE_PATH}/{args.task_id}/kill")
    print_json(result)
    return 0


__all__ = ["RESOURCES", "register"]
