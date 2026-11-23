"""Consumer contracts use a domain-neutral installed extension fixture."""
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from qwenpaw_data.host.core import applications as registry
from qwenpaw_data.host.core.api.models.trace import PresentationSchema


@pytest.fixture(autouse=True)
def reset_discovery():
    registry.applications.cache_clear()
    yield
    registry.applications.cache_clear()


def test_discovery_rejects_duplicates_and_unknown_contract(monkeypatch):
    app = registry.HostApplication()
    entry = SimpleNamespace(name="sample", load=lambda: app)
    monkeypatch.setattr(registry, "entry_points", lambda **_: [entry])
    assert registry.application_for("sample") is app
    assert registry.application_for("missing") is None
    registry.applications.cache_clear()
    monkeypatch.setattr(registry, "entry_points", lambda **_: [entry, entry])
    with pytest.raises(RuntimeError, match="Duplicate"):
        registry.applications()
    registry.applications.cache_clear()
    app.api_version = 2
    monkeypatch.setattr(registry, "entry_points", lambda **_: [entry])
    with pytest.raises(RuntimeError, match="Unsupported"):
        registry.applications()


def test_presentation_preserves_namespaced_payload_without_domain_schema():
    value = dict(card_type="text", caption="Sample", extensions={
        "org.example.analysis": {"version": 1, "data": {"choices": ["a", "b"]}}
    })
    result = PresentationSchema.model_validate(value).model_dump()
    assert result["extensions"] == value["extensions"]
    with pytest.raises(ValidationError):
        PresentationSchema.model_validate({**value, "extensions": {
            "org.example.analysis": {"version": 0, "data": {}}
        }})


@pytest.mark.asyncio
async def test_base_extension_leaves_native_turn_and_questions_unchanged():
    app = registry.HostApplication()
    turn = object()
    assert await app.prepare_turn(None, turn) is turn
    assert await app.handle_external(None, None) is None
    assert app.tools(None) == []


def test_historical_opaque_presentation_fields_survive_replay():
    raw = {"card_type": "tool", "caption": "History", "legacy_plugin": {"value": 3}}
    assert PresentationSchema.model_validate(raw).model_dump()["legacy_plugin"] == {"value": 3}
