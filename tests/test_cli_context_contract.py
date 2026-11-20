"""The CLI must not promise filters the Context service silently ignores."""

import pytest

from qwenpaw_data.cli.commands.semantic import RESOURCES
from qwenpaw_data.cli.main import build_parser
from semantic_config.main import create_app


def test_semantic_cli_filters_exist_in_context_openapi():
    paths = create_app().openapi()["paths"]
    for resource in RESOURCES:
        operation = paths[resource.api_path]["get"]
        query = {
            parameter["name"]
            for parameter in operation.get("parameters", ())
            if parameter["in"] == "query"
        }
        assert {field.key for field in resource.list_filters} <= query, resource.name


def test_semantic_cli_payloads_and_batch_routes_match_context_openapi():
    schema = create_app().openapi()
    for resource in RESOURCES:
        for method, path, fields in (
            ("post", resource.api_path, resource.create_fields),
            ("put", resource.api_path + "/{record_id}", resource.update_fields),
        ):
            if method == "put":
                path = next(
                    key for key, value in schema["paths"].items()
                    if key.startswith(resource.api_path + "/{") and "put" in value
                )
            operation = schema["paths"][path][method]
            ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
            model = schema["components"]["schemas"][ref.rsplit("/", 1)[-1]]
            assert {field.key for field in fields} == set(model["properties"]), resource.name
        if resource.batch_delete:
            path = f"{resource.api_path}/{resource.batch_delete}/{{{resource.batch_delete}_id}}"
            assert "delete" in schema["paths"][path]


def test_semantic_cli_weave_and_import_routes_exist():
    paths = create_app().openapi()["paths"]
    assert "post" in paths["/api/semantic-config/import/excel"]
    assert "post" in paths["/api/semantic-config/weave-task/submit"]
    assert "get" in paths["/api/semantic-config/weave-task/{task_id}"]
    assert "post" in paths["/api/semantic-config/weave-task/{task_id}/kill"]


@pytest.mark.parametrize(("resource", "flag"), [
    ("domain", "--datasource-id"),
    ("dataset", "--type"),
    ("binding", "--datasource-id"),
    ("binding", "--domain-id"),
    ("binding", "--dataset-name"),
    ("formula", "--datasource-id"),
    ("formula", "--domain-id"),
])
def test_cli_rejects_retired_filters(resource, flag):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["semantic", resource, "list", flag, "x"])
    assert exc.value.code == 2
