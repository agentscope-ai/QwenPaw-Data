"""The planner emits portable YAML and never overwrites an existing plan."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from qwenpaw_data.cli.commands import plan
from qwenpaw_data.cli.main import build_parser
from qwenpaw_data.host.core.domain.sop import SOP


async def test_plan_renders_yaml_without_runtime_state(monkeypatch, capsys):
    sop = SOP.from_dict(
        {
            "name": "Plan",
            "description": "Discover",
            "expected_outcome": "Evidence",
            "nodes": [
                {
                    "name": "Inspect",
                    "description": "Read metadata",
                    "expected_outcome": "Schema",
                }
            ],
        }
    )

    class Client:
        create_session = AsyncMock(return_value="s1")
        plan = AsyncMock(return_value=sop)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    client = Client()
    monkeypatch.setattr(plan, "create_client", lambda _: client)
    args = build_parser().parse_args(
        ["plan", "--host-url", "http://host", "Inspect sales"]
    )
    assert await plan.handle(args) == 0
    output = capsys.readouterr()
    assert yaml.safe_load(output.out) == sop.to_dict()
    assert "Session: s1" in output.err
    client.plan.assert_awaited_once_with("s1", "Inspect sales")


async def test_plan_refuses_overwrite_before_model_call(tmp_path, monkeypatch):
    target = tmp_path / "existing.yaml"
    target.write_text("preserve this", encoding="utf-8")

    def unexpected(_):
        pytest.fail("Existing output must be rejected before creating a client")

    monkeypatch.setattr(plan, "create_client", unexpected)
    args = SimpleNamespace(output=target, file=None, prompt=["Inspect"])
    with pytest.raises(ValueError, match="overwrite"):
        await plan.handle(args)
    assert target.read_text(encoding="utf-8") == "preserve this"


async def test_existing_session_datasource_is_not_silently_overridden():
    args = build_parser().parse_args(
        [
            "plan",
            "--host-url",
            "http://host",
            "--session-id",
            "s1",
            "--datasource-id",
            "another-db",
            "Inspect",
        ]
    )
    with pytest.raises(ValueError, match="session datasource"):
        await plan.handle(args)
