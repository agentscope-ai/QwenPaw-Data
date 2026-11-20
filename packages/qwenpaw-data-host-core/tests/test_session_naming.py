# -*- coding: utf-8 -*-
from __future__ import annotations

import re

import pytest

from qwenpaw_data.host.core.api.mappers import session_to_schema
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.db.engine import TEST_TENANT_ID, TEST_USER_ID, TEST_WORKSPACE_ID
from qwenpaw_data.host.core.utils.session_naming import (
    derive_session_title,
    session_display_code,
)


def test_derive_session_title_normalizes_and_bounds_input() -> None:
    assert derive_session_title("  Product X\n March   GAAP anomalies?  ") == (
        "Product X March GAAP anomalies?"
    )
    title = derive_session_title("a" * 80)
    assert title == f"{'a' * 59}…"
    assert len(title) == 60


def test_first_chat_names_an_untitled_session_without_overwriting_a_title() -> None:
    identity = Identity(user_id=TEST_USER_ID, tenant_id=TEST_TENANT_ID, workspace_id=TEST_WORKSPACE_ID)
    untitled = Session.create(identity=identity)
    untitled.open_chat(
        text="Show me Product X GAAP for March",
        datasource_id=None,
        has_active_chat=False,
    )
    assert untitled.title == "Show me Product X GAAP for March"

    named = Session.create(identity=identity, title="March review")
    named.open_chat(
        text="This must not replace the title",
        datasource_id=None,
        has_active_chat=False,
    )
    assert named.title == "March review"


def test_later_chat_does_not_rename_a_session_after_title_is_cleared() -> None:
    session = Session.create(identity=Identity(
        user_id=TEST_USER_ID, tenant_id=TEST_TENANT_ID, workspace_id=TEST_WORKSPACE_ID,
    ))
    session.open_chat(text="First question", datasource_id=None, has_active_chat=False)
    session.title = ""
    session.open_chat(text="Second question", datasource_id=None, has_active_chat=False)
    assert session.title == ""
    assert session.chat_count == 2


def test_rejected_chat_does_not_change_title_or_chat_count() -> None:
    session = Session.create(identity=Identity(
        user_id=TEST_USER_ID, tenant_id=TEST_TENANT_ID, workspace_id=TEST_WORKSPACE_ID,
    ))
    with pytest.raises(RuntimeError, match="CONFLICT"):
        session.open_chat(text="Must not name", datasource_id=None, has_active_chat=True)
    assert session.title == ""
    assert session.chat_count == 0


def test_display_code_is_stable_hex_and_exposed_by_the_api_mapper() -> None:
    session = Session.create(identity=Identity(user_id=TEST_USER_ID, tenant_id=TEST_TENANT_ID, workspace_id=TEST_WORKSPACE_ID))
    code = session_display_code(session.id)
    assert re.fullmatch(r"[0-9A-F]{6}", code)
    assert session_display_code(session.id) == code
    assert session_to_schema(session, has_active_chat=False).display_code == code
