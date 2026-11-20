# -*- coding: utf-8 -*-
from __future__ import annotations

import uuid

from .base import LiveAPITestCase


class TestSessionLive(LiveAPITestCase):
    """Session create / delete against a running Host."""

    def test_create_and_delete_session(self) -> None:
        title = "live-unittest-session"
        # 创建回执：新建 Session 关键字段。
        session = self.create_session(title=title)
        session_id = session["id"]
        self.assertTrue(session_id.startswith("ses-"))
        self.assertEqual(session["title"], title)
        self.assertEqual(session["status"], "idle")
        self.assertEqual(session["chat_count"], 0)
        self.assertEqual(session["channel"], "console")
        self.assertIsNone(session.get("datasource_id"))
        self.assertNotIn("user_id", session)
        self.assertNotIn("tenant_id", session)
        self.assertNotIn("workspace_id", session)
        self.assertTrue(session.get("created_at"))
        self.assertTrue(session.get("updated_at"))

        # 读取核对：GET 与创建结果一致。
        got = self.get_session(session_id)
        self.assertEqual(got["id"], session_id)
        self.assertEqual(got["title"], title)
        self.assertEqual(got["status"], "idle")
        self.assertEqual(got["chat_count"], 0)
        self.assertEqual(got["channel"], "console")

        # 删除核对：删后 404，再删仍成功（幂等）。
        self.delete_session(session_id)
        missing = self.client.get(f"/api/sessions/{session_id}")
        self.assertEqual(missing.status_code, 404, missing.text)
        self.delete_session(session_id)

    def test_multiple_sessions_isolated(self) -> None:
        """Two sessions coexist; deleting one does not affect the other."""
        tag = uuid.uuid4().hex[:8]
        title_a = f"live-unittest-multi-a-{tag}"
        title_b = f"live-unittest-multi-b-{tag}"
        a = self.create_session(title=title_a)
        b = self.create_session(title=title_b)
        id_a, id_b = a["id"], b["id"]
        self.assertNotEqual(id_a, id_b)

        # 列表核对：按 title 片段能同时搜到两条。
        listed = self.list_sessions(search_text=tag)
        ids = {item["id"] for item in listed["items"]}
        self.assertEqual(ids, {id_a, id_b})
        self.assertEqual(listed["total"], 2)

        # 隔离核对：各自 GET 标题正确。
        self.assertEqual(self.get_session(id_a)["title"], title_a)
        self.assertEqual(self.get_session(id_b)["title"], title_b)

        # 删 A 不影响 B；再删 B。
        self.delete_session(id_a)
        self.assertEqual(self.client.get(f"/api/sessions/{id_a}").status_code, 404)
        still_b = self.get_session(id_b)
        self.assertEqual(still_b["id"], id_b)
        self.assertEqual(still_b["title"], title_b)

        self.delete_session(id_b)
        self.assertEqual(self.client.get(f"/api/sessions/{id_b}").status_code, 404)
