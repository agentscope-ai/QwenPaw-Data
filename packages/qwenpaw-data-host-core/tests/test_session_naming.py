# -*- coding: utf-8 -*-
from __future__ import annotations

import re

from qwenpaw_data.host.core.api.mappers import session_to_schema
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.session import Session
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
    identity = Identity.anonymous()
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


def test_display_code_is_stable_hex_and_exposed_by_the_api_mapper() -> None:
    session = Session.create(identity=Identity.anonymous())
    code = session_display_code(session.id)
    assert re.fullmatch(r"[0-9A-F]{6}", code)
    assert session_display_code(session.id) == code
    assert session_to_schema(session, has_active_chat=False)["display_code"] == code
