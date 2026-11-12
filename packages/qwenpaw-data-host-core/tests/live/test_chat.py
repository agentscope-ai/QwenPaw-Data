# -*- coding: utf-8 -*-
from __future__ import annotations

from .base import LiveAPITestCase


class TestChatLive(LiveAPITestCase):
    """Console chat + SSE + session bundle against a running Host."""

    session_id: str

    def setUp(self) -> None:
        session = self.create_session(title="live-unittest-chat")
        self.session_id = session["id"]

    def tearDown(self) -> None:
        if not getattr(self, "session_id", None):
            return
        try:
            snap = self.get_snapshot(self.session_id)
            for chat in snap.get("chats") or []:
                if chat.get("status") == "running":
                    self.wait_chat_not_running(
                        self.session_id, chat["id"], timeout_s=self.sse_timeout_s
                    )
            self.delete_session(self.session_id)
        except Exception:
            self.client.delete(f"/api/sessions/{self.session_id}")

    def test_chat_subscribe_and_inspect(self) -> None:
        prompt = (
            "请用 Write 工具在 workspace 根目录写一个 hello.txt，"
            "内容为 hi，写完后用一句话确认。"
        )
        # 开聊回执：running 且尚无 SSE 事件。
        chat = self.start_chat(session_id=self.session_id, text=prompt)
        chat_id = chat["id"]
        self.assertEqual(chat.get("status"), "running")
        self.assertEqual(chat.get("last_sequence_number"), -1)

        # 实时订流核对：连上后须收到 response 终态（可能竞态错过开头几条）。
        live_events = self.subscribe_events_until_terminal(
            session_id=self.session_id,
            chat_id=chat_id,
            after_sequence_number=-1,
        )
        self.assertTrue(live_events)
        terminal = [e for e in live_events if e.get("object") == "response"][-1]
        self.assertIn(terminal.get("status"), ("completed", "failed", "cancelled"))
        self.assertEqual(terminal.get("chat_id"), chat_id)

        # 事件流核对：终态后全量 replay，序号从 0 起且含 message；有工具则核对 source_id。
        replay = self.subscribe_events_until_terminal(
            session_id=self.session_id,
            chat_id=chat_id,
            after_sequence_number=-1,
        )
        self.assertGreaterEqual(len(replay), 2)
        by_seq = sorted(replay, key=lambda e: e["sequence_number"])
        self.assertEqual(by_seq[0].get("sequence_number"), 0)
        self.assertEqual(by_seq[0].get("object"), "response")
        self.assertEqual(by_seq[0].get("status"), "created")
        self.assertEqual(by_seq[1].get("object"), "response")
        self.assertEqual(by_seq[1].get("status"), "in_progress")

        objects = {e.get("object") for e in replay}
        self.assertIn("message", objects)

        plugin_calls = [
            e
            for e in replay
            if e.get("object") == "message" and e.get("type") == "plugin_call"
        ]
        plugin_outputs = [
            e
            for e in replay
            if e.get("object") == "message" and e.get("type") == "plugin_call_output"
        ]
        if plugin_calls and plugin_outputs:
            call_sid = plugin_calls[0].get("source_id")
            out_sid = plugin_outputs[0].get("source_id")
            self.assertTrue(call_sid)
            self.assertEqual(call_sid, out_sid)
            self.assertNotEqual(plugin_calls[0].get("id"), plugin_outputs[0].get("id"))

        # 落库视图核对：snapshot 含本 chat，trace 结构完整。
        bundle = self.fetch_session_bundle(self.session_id)
        self.assertEqual(bundle["session"]["id"], self.session_id)
        chat_ids = {c["id"] for c in bundle["chats"]}
        self.assertIn(chat_id, chat_ids)
        self.assertIn(chat_id, bundle["traces"])
        self.assertIn("messages", bundle["traces"][chat_id])

    def test_multiple_chats_sequential(self) -> None:
        """Same session: second chat only after first finishes; histories isolated."""
        # 第一轮开聊。
        chat1 = self.start_chat(
            session_id=self.session_id,
            text="只回复一个字：好",
        )
        id1 = chat1["id"]
        self.assertEqual(chat1.get("sequence"), 1)
        self.assertEqual(chat1.get("status"), "running")

        # 活动 Chat 约束：running 时再开一轮应 CONFLICT。
        conflict = self.client.post(
            "/api/console/chat",
            json={
                "session_id": self.session_id,
                "text": "只回复一个字：行",
                "datasource_id": self.datasource_id,
            },
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)
        body = conflict.json()
        detail = body.get("detail") if isinstance(body.get("detail"), dict) else body
        self.assertEqual(detail.get("code"), "CONFLICT")

        self.subscribe_events_until_terminal(
            session_id=self.session_id,
            chat_id=id1,
            after_sequence_number=-1,
        )
        self.wait_chat_not_running(self.session_id, id1)

        # 第一轮终态后再开第二轮。
        chat2 = self.start_chat(
            session_id=self.session_id,
            text="只回复一个字：行",
        )
        id2 = chat2["id"]
        self.assertNotEqual(id1, id2)
        self.assertEqual(chat2.get("sequence"), 2)
        self.assertEqual(chat2.get("datasource_id"), chat1.get("datasource_id"))

        self.subscribe_events_until_terminal(
            session_id=self.session_id,
            chat_id=id2,
            after_sequence_number=-1,
        )
        self.wait_chat_not_running(self.session_id, id2)

        # 落库视图核对：snapshot 两条 chat + 各自 trace。
        bundle = self.fetch_session_bundle(self.session_id)
        self.assertEqual(bundle["session"]["chat_count"], 2)
        chat_ids = {c["id"] for c in bundle["chats"]}
        self.assertEqual(chat_ids, {id1, id2})
        by_id = {c["id"]: c for c in bundle["chats"]}
        self.assertEqual(by_id[id1]["sequence"], 1)
        self.assertEqual(by_id[id2]["sequence"], 2)
        self.assertIn("messages", bundle["traces"][id1])
        self.assertIn("messages", bundle["traces"][id2])

        # 事件流隔离核对：各 chat 全量 replay，chat_id 不串、序号各自从 0 起。
        replay1 = self.subscribe_events_until_terminal(
            session_id=self.session_id,
            chat_id=id1,
            after_sequence_number=-1,
        )
        replay2 = self.subscribe_events_until_terminal(
            session_id=self.session_id,
            chat_id=id2,
            after_sequence_number=-1,
        )
        self.assertTrue(replay1)
        self.assertTrue(replay2)
        self.assertTrue(all(e.get("chat_id") == id1 for e in replay1))
        self.assertTrue(all(e.get("chat_id") == id2 for e in replay2))

        head1 = sorted(replay1, key=lambda e: e["sequence_number"])[0]
        head2 = sorted(replay2, key=lambda e: e["sequence_number"])[0]
        self.assertEqual(head1.get("sequence_number"), 0)
        self.assertEqual(head2.get("sequence_number"), 0)
        self.assertEqual(head1.get("object"), "response")
        self.assertEqual(head2.get("object"), "response")
