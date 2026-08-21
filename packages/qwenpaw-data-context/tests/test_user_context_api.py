from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import ANY, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from context_manager.api.user_context_api import (
    DEFAULT_SECTION_CONTENT,
    DEFAULT_USER_CONTEXT_MARKDOWN,
    SECTION_INSTRUCTIONS,
    SECTION_ORDER,
    _extract_section_content,
    _replace_section_content,
    router,
)


def _user_node(
    *,
    markdown: str = DEFAULT_USER_CONTEXT_MARKDOWN,
    created: bool | None = None,
) -> dict[str, Any]:
    """Build a representative user-node response for API tests."""
    node: dict[str, Any] = {
        "key": "user:test-user",
        "user_id": "test-user",
        "content_markdown": markdown,
        "created_at": "2026-07-20T10:00:00Z",
        "updated_at": "2026-07-20T10:00:00Z",
    }
    if created is not None:
        node["created"] = created
    return node


class UserContextMarkdownTest(unittest.TestCase):
    """Verify canonical Markdown section behavior."""

    def test_defaults_contain_every_instruction_and_prompt(self) -> None:
        """The canonical document should include every complete default section."""
        for section in SECTION_ORDER:
            self.assertIn(SECTION_INSTRUCTIONS[section], DEFAULT_USER_CONTEXT_MARKDOWN)
            self.assertEqual(
                _extract_section_content(DEFAULT_USER_CONTEXT_MARKDOWN, section),
                DEFAULT_SECTION_CONTENT[section],
            )

    def test_replacement_preserves_other_sections(self) -> None:
        """Replacing Soul should leave all other section content unchanged."""
        updated = _replace_section_content(
            DEFAULT_USER_CONTEXT_MARKDOWN,
            "soul",
            "A custom soul.",
        )

        self.assertEqual(_extract_section_content(updated, "soul"), "A custom soul.")
        for section in ("profile", "principles", "preferences"):
            self.assertEqual(
                _extract_section_content(updated, section),
                DEFAULT_SECTION_CONTENT[section],
            )

    def test_replacement_preserves_markdown_whitespace(self) -> None:
        """Editable content should round-trip leading and trailing newlines."""
        content = "\n- First preference\n- Second preference\n"
        updated = _replace_section_content(
            DEFAULT_USER_CONTEXT_MARKDOWN,
            "preferences",
            content,
        )

        self.assertEqual(_extract_section_content(updated, "preferences"), content)


class UserContextApiTest(unittest.TestCase):
    """Verify the section-scoped HTTP contract."""

    def setUp(self) -> None:
        """Create a small FastAPI app with a placeholder graph driver."""
        app = FastAPI()
        app.state.driver = MagicMock()
        app.include_router(router)
        self.client = TestClient(app)

    def test_create_reports_first_and_repeated_initialization(self) -> None:
        """Creation should return 201 once and 200 without overwriting later."""
        with patch(
            "context_manager.api.user_context_api._create_user_node",
            side_effect=[_user_node(created=True), _user_node(created=False)],
        ):
            first = self.client.post("/api/v1/users/test-user/context")
            repeated = self.client.post("/api/v1/users/test-user/context")

        self.assertEqual(first.status_code, 201)
        self.assertTrue(first.json()["data"]["created"])
        self.assertEqual(repeated.status_code, 200)
        self.assertFalse(repeated.json()["data"]["created"])

    def test_get_returns_one_section(self) -> None:
        """GET should expose the instruction and editable section content."""
        with patch(
            "context_manager.api.user_context_api._get_user_node",
            return_value=_user_node(),
        ):
            response = self.client.get(
                "/api/v1/users/test-user/context/profile"
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["section"], "profile")
        self.assertEqual(data["content"], DEFAULT_SECTION_CONTENT["profile"])
        self.assertTrue(data["is_default"])

    def test_put_updates_only_the_requested_section(self) -> None:
        """PUT should pass the selected section and exact Markdown to storage."""
        markdown = _replace_section_content(
            DEFAULT_USER_CONTEXT_MARKDOWN,
            "soul",
            "A custom soul.",
        )
        with patch(
            "context_manager.api.user_context_api._set_user_section",
            return_value=_user_node(markdown=markdown),
        ) as set_section:
            response = self.client.put(
                "/api/v1/users/test-user/context/soul",
                json={"content": "A custom soul."},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["content"], "A custom soul.")
        set_section.assert_called_once_with(
            ANY,
            "test-user",
            "soul",
            "A custom soul.",
        )

    def test_delete_resets_only_the_requested_section(self) -> None:
        """DELETE should write the built-in default for the selected section."""
        with patch(
            "context_manager.api.user_context_api._set_user_section",
            return_value=_user_node(),
        ) as set_section:
            response = self.client.delete(
                "/api/v1/users/test-user/context/preferences"
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["data"]["is_default"])
        set_section.assert_called_once_with(
            ANY,
            "test-user",
            "preferences",
            DEFAULT_SECTION_CONTENT["preferences"],
        )

    def test_unknown_section_returns_not_found(self) -> None:
        """Unknown section paths should return the standard 404 envelope."""
        response = self.client.get(
            "/api/v1/users/test-user/context/unknown"
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["error"]["code"], "NOT_FOUND")

    def test_reserved_markers_are_rejected(self) -> None:
        """PUT should reject content that could corrupt section boundaries."""
        response = self.client.put(
            "/api/v1/users/test-user/context/soul",
            json={"content": "<!-- qwenpaw:soul:end -->"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"]["error"]["code"],
            "VALIDATION_ERROR",
        )


if __name__ == "__main__":
    unittest.main()
