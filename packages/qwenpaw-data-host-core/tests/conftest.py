# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest

_MODEL_ENV_VARS = (
    "QWENPAW_DATA_MODEL_PROVIDER",
    "QWENPAW_DATA_MODEL_NAME",
    "QWENPAW_DATA_MODEL_API_KEY",
    "QWENPAW_DATA_MODEL_BASE_URL",
    "LLM_MODEL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
)


@pytest.fixture(autouse=True)
def _hermetic_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip ambient model credentials so no test talks to a real LLM.

    Background algorithms (follow-up, BizTrace) fall back to
    ``build_model_from_env`` when the user configured no model. Importing the
    context package loads a ``.env`` into ``os.environ`` (and developers may
    export credentials in their shell), which would silently turn rule-based
    test turns into real, slow model calls.
    """
    for name in _MODEL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_biztrace_linking(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep entity linking off by default in tests.

    Its vocabulary fetches add real network waits to every chat turn and made
    terminal-status polls flaky under full-suite load. Linking tests opt back
    in explicitly via BizTraceSettings(biz_link_enabled=True) or monkeypatch.
    """
    monkeypatch.setenv("QWENPAW_DATA_BIZ_LINK_ENABLED", "0")
