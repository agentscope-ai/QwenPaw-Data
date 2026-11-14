"""Verify per-field clocks and late user messages."""

import json

from context_manager.user.algo.reconciliation import merge_profile

OLD = "2026-08-31T10:00:00.000000+00:00"
NEW = "2026-08-31T11:00:00.000000+00:00"


def test_profile_clocks_are_independent_and_protect_explicit_clears():
    original = {
        "display_name": "New name",
        "timezone": None,
        "_evidence_at": {"display_name": NEW, "timezone": NEW},
    }
    merged = json.loads(
        merge_profile(
            original,
            {"display_name": "Old name", "locale": "en-GB", "timezone": "UTC"},
            stamp=OLD,
        )
    )
    assert merged["display_name"] == "New name" and merged["timezone"] is None
    assert merged["locale"] == "en-GB"
    assert merged["_evidence_at"] == {**original["_evidence_at"], "locale": OLD}
    assert (
        json.loads(merge_profile(original, {"display_name": "Another name"}, stamp=NEW))
        == original
    )
