"""Persisted preferences survive migration without ambient credentials/defaults."""

from copy import deepcopy
from datetime import datetime, timezone

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from qwenpaw_data.host.core.migration.credentials import CredentialRewriter
from qwenpaw_data.host.core.migration.legacy_json import MigrationError
from qwenpaw_data.host.core.migration.preferences import convert_preferences
from qwenpaw_data.host.core.providers import provider_registry


NOW = datetime(2026, 11, 14, tzinfo=timezone.utc)
TARGET_KEY = bytes(range(32))
USERS = {"old": {"user_id": "new", "tenant_id": "tenant", "workspace_id": "workspace"}}
CUSTOM = "openai/fixture/org:model"
DOCUMENT = {
    "providers": {
        "openai": {
            "api_key_enc": "synthetic-private-key",
            "base_url": "https://fixture.invalid/v1",
        },
    },
    "models": {
        CUSTOM: {
            "source": "extra",
            "name": "Private custom model",
            "thinking_enabled": False,
            "generate_kwargs": {"temperature": 0.25, "nested": {"retain": True}},
        },
        "openai/gpt-4o": {
            "source": "override",
            "name": "Private light model",
            "thinking_enabled": True,
        },
    },
    "active": {
        "default_provider_id": "openai",
        "default_model_id": "fixture/org:model",
        "light_provider_id": "openai",
        "light_model_id": "gpt-4o",
    },
    "runtime": {
        "react_max_iters": 500,
        "llm_retry_enabled": False,
        "llm_max_retries": 0,
        "context_reserve_ratio": 0.2,
        "tool_result_limit": 1000,
        "tool_timeout_enabled": False,
        "tool_timeout_seconds": 45.5,
        "tool_confirmation_level": "all",
    },
}


def convert(document=DOCUMENT, **overrides):
    kwargs = dict(
        old_user_id="old",
        users=USERS,
        credentials=CredentialRewriter(source_key=None, target_key=TARGET_KEY),
        registry=provider_registry,
        converted_at=NOW,
        target_context_trigger_ratio=0.8,
        target_context_reserve_ratio=0.1,
        model_context_sizes={CUSTOM: 77788},
    )
    kwargs.update(overrides)
    return convert_preferences(document, **kwargs)


def test_all_preference_tables_are_prepared_without_mutating_or_disclosing_source():
    source = deepcopy(DOCUMENT)
    rows = convert(source)
    assert source == DOCUMENT
    assert {r.table for r in rows} == {
        "user_providers",
        "user_provider_models",
        "user_active_models",
        "user_runtime_settings",
    }
    assert all(
        r.values["user_id"] == "new" and r.values["updated_at"] == NOW for r in rows
    )
    assert all(
        "synthetic-private" not in repr(r) and "Private" not in repr(r) for r in rows
    )
    model = next(r for r in rows if r.values.get("model_id") == "fixture/org:model")
    assert model.values["context_size"] == 77788
    assert model.values["thinking_enabled"] is False
    assert (
        model.values["generate_kwargs_json"]
        == DOCUMENT["models"][CUSTOM]["generate_kwargs"]
    )
    assert model.notices == ("model_context_size_added",)
    runtime = next(r.values for r in rows if r.table == "user_runtime_settings")
    assert runtime["context_trigger_ratio"] is None  # Don't materialize defaults.
    assert runtime["llm_max_retries"] == 0 and runtime["tool_timeout_enabled"] is False


@pytest.mark.parametrize(
    "sizes",
    [
        None,
        {},
        {CUSTOM: True},
        {CUSTOM: 0},
        {CUSTOM: 2**31},
        {CUSTOM: 200, "typo/model": 20},
        [],
        {CUSTOM: "200"},
    ],
)
def test_missing_invalid_or_unused_context_size_additions_are_rejected(sizes):
    with pytest.raises(MigrationError):
        convert(model_context_sizes=sizes)


def test_stored_context_size_is_preserved_not_silently_replaced():
    doc = deepcopy(DOCUMENT)
    doc["models"][CUSTOM]["context_size"] = 60000
    with pytest.raises(MigrationError, match="unused"):
        convert(doc)
    rows = convert(doc, model_context_sizes={})
    assert (
        next(
            r.values["context_size"]
            for r in rows
            if r.values.get("model_id") == "fixture/org:model"
        )
        == 60000
    )


@pytest.mark.parametrize(
    "active",
    [
        {"light_provider_id": "openai", "light_model_id": "gpt-4o"},
        {"default_provider_id": "openai"},
        {"default_provider_id": "dashscope", "default_model_id": "qwen-plus"},
        {"default_provider_id": "openai", "default_model_id": "removed-model"},
        {"default_provider_id": "openai", "default_model_id": " gpt-4o"},
    ],
)
def test_unresolvable_active_selection_does_not_fall_back(active):
    with pytest.raises(MigrationError):
        convert({**DOCUMENT, "active": active})


@pytest.mark.parametrize(
    "level,field",
    [
        ("document", "new-key"),
        ("provider", "private-secret-field"),
        ("model", "new-option"),
        ("active", "new-selection"),
        ("runtime", "new-setting"),
    ],
)
def test_unknown_fields_cannot_disappear(level, field):
    doc = deepcopy(DOCUMENT)
    selected = {
        "document": doc,
        "provider": doc["providers"]["openai"],
        "model": doc["models"][CUSTOM],
        "active": doc["active"],
        "runtime": doc["runtime"],
    }[level]
    selected[field] = "private-value-never-log"
    with pytest.raises(MigrationError) as exc:
        convert(doc)
    assert "private-value" not in str(exc.value) and field not in str(exc.value)


@pytest.mark.parametrize(
    "runtime",
    [
        {"llm_retry_enabled": "false"},
        {"react_max_iters": True},
        {"llm_max_retries": -1},
        {"tool_timeout_seconds": float("inf")},
        {"tool_confirmation_level": "unknown"},
        {"context_reserve_ratio": 0.85},
        {"context_trigger_ratio": 0.05},
    ],
)
def test_invalid_runtime_or_conflicting_effective_ratios_are_refused(runtime):
    with pytest.raises(MigrationError):
        convert({**DOCUMENT, "runtime": runtime})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"users": {}},
        {"converted_at": datetime(2026, 11, 14)},
        {"target_context_trigger_ratio": None},
        {"target_context_reserve_ratio": 0.85},
        {"registry": None},
        {"credentials": None},
    ],
)
def test_explicit_target_context_is_required(kwargs):
    with pytest.raises(MigrationError):
        convert(**kwargs)


def test_removed_catalog_model_requires_remapping_and_no_unknown_provider_is_dropped():
    doc = deepcopy(DOCUMENT)
    doc["models"]["openai/missing-old-catalog-model"] = {"source": "override"}
    with pytest.raises(MigrationError, match="Removed catalog model"):
        convert(doc)
    doc = deepcopy(DOCUMENT)
    doc["providers"]["missing-provider"] = {"api_key_enc": "private"}
    with pytest.raises(MigrationError, match="unavailable"):
        convert(doc)


@pytest.mark.parametrize(
    "empty",
    [
        {},
        {"providers": {}, "models": {}, "active": None},
        {"active": {}},
        {
            "active": {
                "default_provider_id": None,
                "default_model_id": None,
                "light_provider_id": None,
                "light_model_id": None,
            }
        },
    ],
)
def test_empty_preferences_do_not_create_fake_defaults(empty):
    assert convert(empty, model_context_sizes={}) == ()


def test_wrong_master_key_fails_entire_conversion():
    doc = deepcopy(DOCUMENT)
    doc["providers"]["openai"]["api_key_enc"] = CredentialRewriter(
        source_key=None, target_key=bytes(range(32, 64))
    ).rewrite("secret")
    with pytest.raises(MigrationError, match="authenticate"):
        convert(
            doc,
            credentials=CredentialRewriter(
                source_key=TARGET_KEY, target_key=TARGET_KEY
            ),
        )


def test_ambient_preferences_are_never_consulted(monkeypatch):
    from qwenpaw_data.host.core import settings

    def forbidden():
        pytest.fail("conversion consulted ambient settings")

    monkeypatch.setattr(settings, "get_settings", forbidden)
    monkeypatch.setenv("QWENPAW_DATA_RUNTIME_CONTEXT_RESERVE_RATIO", "0.99")
    monkeypatch.setenv("QWENPAW_DATA_MODEL_API_KEY", "unrelated-private-key")
    assert len(convert()) == 5


async def test_sql_reload_resolves_native_default_and_light_models(
    tmp_path, monkeypatch
):
    from qwenpaw_data.host.core.db.tables import Base
    from qwenpaw_data.host.core.domain.identity import Identity
    from qwenpaw_data.host.core.repositories.preferences import PreferencesRepository
    from qwenpaw_data.host.core.settings import clear_settings

    monkeypatch.setenv("QWENPAW_DATA_PREFERENCES_MASTER_SECRET", TARGET_KEY.hex())
    monkeypatch.setenv("QWENPAW_DATA_RUNTIME_CONTEXT_TRIGGER_RATIO", "0.8")
    clear_settings()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'prefs.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            for row in convert():
                await conn.execute(
                    insert(Base.metadata.tables[row.table]).values(**row.values)
                )
        async with factory() as db:
            loaded = await PreferencesRepository(db, Identity(**USERS["old"])).load()
            assert (
                loaded.models[("openai", "fixture/org:model")].generate_kwargs
                == DOCUMENT["models"][CUSTOM]["generate_kwargs"]
            )
            resolved = loaded._build()  # No environment fallback to hide failure.
            assert resolved.default.model_id == "fixture/org:model"
            assert resolved.default.api_key == "synthetic-private-key"
            assert resolved.default.context_size == 77788
            assert resolved.default.thinking_enabled is False
            assert resolved.light.model_id == "gpt-4o"
            assert resolved.light.thinking_enabled is True
            assert resolved.react_max_iters == 500 and resolved.llm_max_retries == 0
            assert resolved.tool_confirmation_level == "all"
            assert (
                resolved.context_trigger_ratio == 0.8
                and resolved.context_reserve_ratio == 0.2
            )
    finally:
        await engine.dispose()
