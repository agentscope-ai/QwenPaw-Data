# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import OperationalError

from qwenpaw_data.host.core.api.app import create_app
from qwenpaw_data.host.core.api.models.submissions import SubmitRunRequest
from qwenpaw_data.host.core.api.routers import submissions as routes
from qwenpaw_data.host.core.db.tables import ChatRow, SessionRow, SubmissionRow
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.runtime.chat_runtime import ChatRuntime
from qwenpaw_data.host.core.runtime.registry import get_runtime_registry
from qwenpaw_data.host.core.store import submissions as submission_store
from qwenpaw_data.host.core.stream.output_stream import OutputStream

PAYLOAD = {"submission_id": "sub_test", "text": "分析收入", "datasource_id": "ds1"}
URL = "/api/v1/submissions"
ANSWER = {
    "protocol_version": 1,
    "command_id": "cmd_answer_1",
    "kind": "answer",
    "request_id": "clarification_1",
    "answers": [
        {
            "question": "Which period?",
            "selected_options": ["Q1"],
        }
    ],
}


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for name in ("QWENPAW_DATA_API_TOKEN", "QWENPAW_DATA_DB_URL", "QWENPAW_DATA_STORE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def starts(monkeypatch):
    runs = []

    async def run(self, chat_id, *, identity):
        runs.append(chat_id)

    monkeypatch.setattr(ChatRuntime, "run", run)
    return runs


@asynccontextmanager
async def service(home, **transport_options):
    app = create_app(home=home, model=object())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=app,
                client=("127.0.0.1", 1234),
                **transport_options,
            ),
            base_url="http://testserver",
        ) as http:
            yield http, app.state.service


async def counts(state):
    async with state.submissions._sessions() as db:
        return [
            await db.scalar(select(func.count()).select_from(table))
            for table in (SubmissionRow, SessionRow, ChatRow)
        ]


async def accept_without_scheduling(state):
    body = SubmitRunRequest(**PAYLOAD)
    record, created = await state.submissions.submit(
        identity=Identity.anonymous(),
        submission_id=body.submission_id,
        request_digest=body.request_digest(),
        text=body.text,
        datasource_id=body.datasource_id,
        agent_id=body.agent_id,
    )
    assert created
    return record


async def finish(state, run, status="completed"):
    chat = await state.chats.get(run["run_id"])
    stream = OutputStream(
        state.events,
        session_id=chat.session_id,
        chat_id=chat.id,
        identity=chat.identity,
    )
    await stream.text_end(msg_id="result", index=0, text="Revenue is 42.")
    chat.mark_status(
        {"completed": "completed", "failed": "failed", "cancelled": "canceled"}[status]
    )
    await state.chats.save(chat)
    await stream.response(status)


def sse_payloads(response):
    return [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


async def test_concurrent_retries_create_and_start_exactly_one_run(tmp_path, starts):
    async with service(tmp_path) as (http, state):
        responses = await asyncio.gather(
            *[http.post(URL, json=PAYLOAD) for _ in range(12)]
        )
        assert {r.status_code for r in responses} == {202}
        runs = [r.json()["run"] for r in responses]
        assert len({r["session_id"] for r in runs}) == 1
        assert len({r["run_id"] for r in runs}) == 1
        assert starts == [runs[0]["run_id"]]
        assert await counts(state) == [1, 1, 1]
        lookup = (await http.get(f"{URL}/sub_test")).json()
        assert lookup["state"] == "accepted"
        assert lookup["run"]["run_id"] == starts[0]


@pytest.mark.parametrize("datasource_id", ["ds1", None])
async def test_existing_session_turns_preserve_context_and_identity(
    tmp_path, starts, datasource_id
):
    async with service(tmp_path) as (http, state):
        headers = {"X-User-Id": "alice"}
        response = await http.post(
            "/api/v1/sessions", json={"title": "March GAAP"}, headers=headers
        )
        session_id = response.json()["session"]["id"]
        readiness_url = f"/api/v1/sessions/{session_id}/submission-readiness"
        assert (await http.get(readiness_url, headers=headers)).json() == {
            "ready": True
        }
        assert (
            await http.get(readiness_url, headers={"X-User-Id": "bob"})
        ).status_code == 404
        body = {
            **PAYLOAD,
            "datasource_id": datasource_id,
            "session_id": session_id,
            "artifact_comments": [
                {
                    "path": "report.md",
                    "line_start": 1,
                    "line_end": 2,
                    "comment": "Explain this",
                },
            ],
        }
        accepted = await http.post(URL, json=body, headers=headers)
        assert accepted.status_code == 202, accepted.text
        run = accepted.json()["run"]
        assert (await http.get(readiness_url, headers=headers)).json() == {
            "ready": False
        }
        assert run["session_id"] == session_id
        chat = await state.chats.get(run["run_id"])
        assert chat.artifact_comments[0]["comment"] == "Explain this"
        assert chat.datasource_id == datasource_id
        assert (await http.post(URL, json=body, headers=headers)).json()["run"] == run
        assert await counts(state) == [1, 1, 1]
        assert (
            await http.post(
                URL, json={**body, "submission_id": "another"}, headers=headers
            )
        ).status_code == 409
        denied = await http.post(URL, json=body, headers={"X-User-Id": "bob"})
        assert denied.status_code == 404
        assert (
            await http.get(
                f"/api/v1/sessions/{session_id}/chats/{run['run_id']}",
                headers={"X-User-Id": "bob"},
            )
        ).status_code == 404
        await finish(state, run)
        second = await http.post(
            URL, json={**body, "submission_id": "next"}, headers=headers
        )
        assert second.status_code == 202, second.text
        assert second.json()["run"]["session_id"] == session_id
        assert await counts(state) == [2, 1, 2]


async def test_submission_passes_capability_bridge_to_runtime(tmp_path, monkeypatch):
    received = []

    async def run(self, chat_id, *, identity, capability_bridge=None):
        received.append((chat_id, capability_bridge))

    monkeypatch.setattr(ChatRuntime, "run", run)
    bridge = {
        "protocol_version": 1,
        "endpoint": "http://127.0.0.1:8088/api/pawapp-capabilities",
        "token": "x" * 32,
    }
    async with service(tmp_path) as (http, _state):
        response = await http.post(URL, json={**PAYLOAD, "capability_bridge": bridge})

    assert response.status_code == 202
    assert received == [(response.json()["run"]["run_id"], bridge)]


@pytest.mark.parametrize(
    "change",
    [
        {"text": "different"},
        {"datasource_id": "ds2"},
        {"agent_id": "another"},
    ],
)
async def test_reuse_with_changed_inputs_is_conflict(tmp_path, starts, change):
    async with service(tmp_path) as (http, state):
        await http.post(URL, json=PAYLOAD)
        conflict = await http.post(URL, json={**PAYLOAD, **change})
        assert conflict.status_code == 409
        assert await counts(state) == [1, 1, 1]
        assert len(starts) == 1


async def test_separate_submissions_and_identity_namespaces(tmp_path, starts):
    async with service(tmp_path) as (http, state):
        first = (await http.post(URL, json=PAYLOAD)).json()["run"]
        unknown = await http.get(f"{URL}/sub_test", headers={"X-User-Id": "other"})
        assert unknown.json()["state"] == "not_found"
        assert (
            await http.get(f"{URL}/sub_test/events", headers={"X-User-Id": "other"})
        ).status_code == 404
        second = (
            await http.post(URL, json=PAYLOAD, headers={"X-User-Id": "other"})
        ).json()["run"]
        third = (
            await http.post(URL, json={**PAYLOAD, "submission_id": "sub_other"})
        ).json()["run"]
        assert len({r["session_id"] for r in (first, second, third)}) == 3
        assert len(starts) == 3
        assert await counts(state) == [3, 3, 3]


async def test_fault_before_commit_rolls_back_all_rows(tmp_path, starts, monkeypatch):
    async with service(tmp_path, raise_app_exceptions=False) as (http, state):
        original = submission_store._chat_to_row

        def fail(_chat):
            raise RuntimeError("injected failure after receipt flush")

        monkeypatch.setattr(submission_store, "_chat_to_row", fail)
        assert (await http.post(URL, json=PAYLOAD)).status_code == 500
        assert await counts(state) == [0, 0, 0]
        assert (await http.get(f"{URL}/sub_test")).json()["state"] == "not_found"
        assert not starts
        monkeypatch.setattr(submission_store, "_chat_to_row", original)
        assert (await http.post(URL, json=PAYLOAD)).status_code == 202
        assert len(starts) == 1


async def test_lost_http_response_reconciles_original_run(
    tmp_path, starts, monkeypatch
):
    async with service(tmp_path, raise_app_exceptions=False) as (http, state):
        original = routes._lookup_response

        async def fail(*_args):
            raise ConnectionError("response lost after acceptance")

        monkeypatch.setattr(routes, "_lookup_response", fail)
        assert (await http.post(URL, json=PAYLOAD)).status_code == 500
        monkeypatch.setattr(routes, "_lookup_response", original)
        lookup = (await http.get(f"{URL}/sub_test")).json()
        retried = (await http.post(URL, json=PAYLOAD)).json()
        assert lookup == retried
        assert starts == [lookup["run"]["run_id"]]
        assert await counts(state) == [1, 1, 1]


async def test_http_cancellation_cannot_cancel_acceptance(
    tmp_path, starts, monkeypatch
):
    async with service(tmp_path) as (http, state):
        committed = asyncio.Event()
        release = asyncio.Event()
        original = state.submissions.submit

        async def pause(**kwargs):
            accepted = await original(**kwargs)
            committed.set()
            await release.wait()
            return accepted

        monkeypatch.setattr(state.submissions, "submit", pause)
        request = asyncio.create_task(
            routes.submit_run(
                SubmitRunRequest(**PAYLOAD),
                Identity.anonymous(),
                state,
            )
        )
        await asyncio.wait_for(committed.wait(), 5)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        release.set()
        await asyncio.gather(*list(state.tasks))
        assert len(starts) == 1
        assert (await http.get(f"{URL}/sub_test")).json()["run"]["run_id"] == starts[0]


async def test_answer_command_is_durable_idempotent_and_scoped(tmp_path, starts):
    class Runtime:
        def __init__(self):
            self.answers = []

        def answer(self, *, clarification_id, result):
            self.answers.append((clarification_id, result))

    async with service(tmp_path) as (http, _state):
        run = (await http.post(URL, json=PAYLOAD)).json()["run"]
        runtime = Runtime()
        get_runtime_registry().register(run["run_id"], runtime)  # type: ignore[arg-type]
        try:
            first = await http.post(f"{URL}/sub_test/commands", json=ANSWER)
            retry = await http.post(f"{URL}/sub_test/commands", json=ANSWER)
            lookup = await http.get(f"{URL}/sub_test/commands/{ANSWER['command_id']}")
            assert first.status_code == 200
            assert first.json()["state"] == "accepted"
            assert retry.json() == first.json() == lookup.json()
            assert runtime.answers == [
                (
                    "clarification_1",
                    {
                        "status": "answered",
                        "answers": ANSWER["answers"],
                    },
                )
            ]
            conflict = await http.post(
                f"{URL}/sub_test/commands",
                json={
                    **ANSWER,
                    "answers": [{**ANSWER["answers"][0], "selected_options": ["Q2"]}],
                },
            )
            assert conflict.status_code == 409
            assert (
                await http.get(
                    f"{URL}/sub_test/commands/{ANSWER['command_id']}",
                    headers={"X-User-Id": "other"},
                )
            ).status_code == 404
        finally:
            get_runtime_registry().unregister(run["run_id"])


async def test_stale_answer_is_a_stable_rejected_receipt(tmp_path, starts):
    async with service(tmp_path) as (http, _state):
        await http.post(URL, json=PAYLOAD)
        first = await http.post(f"{URL}/sub_test/commands", json=ANSWER)
        retry = await http.post(f"{URL}/sub_test/commands", json=ANSWER)
        assert first.json()["state"] == "rejected"
        assert first.json()["reason"] == "stale_request"
        assert retry.json() == first.json()


async def test_cancel_command_targets_original_run_once(tmp_path, starts):
    class Runtime:
        def __init__(self):
            self.cancels = 0

        async def cancel(self):
            self.cancels += 1

    body = {
        "protocol_version": 1,
        "command_id": "cmd_cancel_1",
        "kind": "cancel",
        "reason": "user_requested",
    }
    async with service(tmp_path) as (http, _state):
        run = (await http.post(URL, json=PAYLOAD)).json()["run"]
        runtime = Runtime()
        get_runtime_registry().register(run["run_id"], runtime)  # type: ignore[arg-type]
        try:
            first = await http.post(f"{URL}/sub_test/commands", json=body)
            retry = await http.post(f"{URL}/sub_test/commands", json=body)
            assert first.json()["state"] == "accepted"
            assert retry.json() == first.json()
            assert runtime.cancels == 1
        finally:
            get_runtime_registry().unregister(run["run_id"])


async def test_prepared_command_queries_as_unknown_after_restart_window(
    tmp_path, starts
):
    async with service(tmp_path) as (http, state):
        record = await accept_without_scheduling(state)
        command, created = await state.submissions.prepare_command(
            record=record,
            command_id=ANSWER["command_id"],
            kind="answer",
            request_digest="a" * 64,
        )
        assert created and command.state == "unknown"
        lookup = await http.get(f"{URL}/sub_test/commands/{ANSWER['command_id']}")
        assert lookup.json()["state"] == "unknown"


async def test_restart_after_commit_before_scheduling_is_interrupted(tmp_path, starts):
    async with service(tmp_path) as (_http, state):
        record = await accept_without_scheduling(state)
        assert not starts
    async with service(tmp_path) as (http, state):
        lookup = (await http.get(f"{URL}/sub_test")).json()
        assert lookup["run"]["run_id"] == record.run_id
        assert lookup["run"]["status"] == "interrupted"
        assert lookup["run"]["error"]["details"]["reason"] == "executor_restarted"
        assert (await http.post(URL, json=PAYLOAD)).json() == lookup
        assert not starts
        events = sse_payloads(await http.get(f"{URL}/sub_test/events"))
        assert len(events) == 1
        assert events[0]["status"] == "cancelled"
    # Recovery itself must be idempotent.
    async with service(tmp_path) as (http, _state):
        assert len(sse_payloads(await http.get(f"{URL}/sub_test/events"))) == 1


async def test_process_exit_after_commit_preserves_receipt(tmp_path, starts):
    # A real abrupt exit: no lifespan teardown, runtime cancellation or engine
    # disposal can accidentally make this recovery test pass.
    script = textwrap.dedent("""
        import asyncio, json, os, sys
        from pathlib import Path
        from qwenpaw_data.host.core.api.models.submissions import SubmitRunRequest
        from qwenpaw_data.host.core.db.engine import create_engine_and_factory, init_db, resolve_db_url
        from qwenpaw_data.host.core.domain.identity import Identity
        from qwenpaw_data.host.core.store.submissions import SQLSubmissionStore

        async def main():
            engine, factory = create_engine_and_factory(resolve_db_url(Path(sys.argv[1])))
            await init_db(engine)
            body = SubmitRunRequest(**json.loads(sys.argv[2]))
            record, _ = await SQLSubmissionStore(factory).submit(
                identity=Identity.anonymous(), submission_id=body.submission_id,
                request_digest=body.request_digest(), text=body.text,
                datasource_id=body.datasource_id, agent_id=body.agent_id,
            )
            print(record.run_id, flush=True)
            os._exit(23)
        asyncio.run(main())
    """)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(tmp_path),
        json.dumps(PAYLOAD),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 23, stderr.decode()
    run_id = stdout.decode().strip()
    async with service(tmp_path) as (http, state):
        lookup = (await http.get(f"{URL}/sub_test")).json()
        assert lookup["state"] == "accepted"
        assert lookup["run"]["run_id"] == run_id
        assert lookup["run"]["status"] == "interrupted"
        assert (await http.post(URL, json=PAYLOAD)).json() == lookup
        assert await counts(state) == [1, 1, 1]
        assert not starts


async def test_disconnect_while_running_replays_only_unconsumed_events(
    tmp_path, starts
):
    class ConnectedRequest:
        async def is_disconnected(self):
            return False

    async with service(tmp_path) as (http, state):
        run = (await http.post(URL, json=PAYLOAD)).json()["run"]
        stream = OutputStream(
            state.events,
            session_id=run["session_id"],
            chat_id=run["run_id"],
            identity=Identity.anonymous(),
        )
        await stream.response_created()
        response = await routes.submission_events(
            ConnectedRequest(),
            "sub_test",
            Identity.anonymous(),
            state,
            -1,
            None,
        )
        iterator = response.body_iterator
        first = await anext(iterator)
        assert "id: 0" in first
        await stream.text_delta(msg_id="result", index=0, text="Revenue")
        live = await asyncio.wait_for(anext(iterator), 5)
        assert "id: 1" in live
        await iterator.aclose()  # Simulate transport disconnect after cursor 1.
        await finish(state, run)
        replay = sse_payloads(
            await http.get(
                f"{URL}/sub_test/events",
                headers={"Last-Event-ID": "1"},
            )
        )
        assert [event["sequence_number"] for event in replay] == [2, 3]
        assert replay[0]["text"] == "Revenue is 42."
        assert replay[1]["status"] == "completed"
        assert len(starts) == 1


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
async def test_terminal_replay_survives_restart_and_cursor(tmp_path, starts, status):
    async with service(tmp_path) as (http, state):
        run = (await http.post(URL, json=PAYLOAD)).json()["run"]
        await finish(state, run, status)
        first = sse_payloads(await http.get(f"{URL}/sub_test/events"))
        assert len(first) == 2
    async with service(tmp_path) as (http, state):
        replay = sse_payloads(await http.get(f"{URL}/sub_test/events"))
        assert replay == first
        after = sse_payloads(
            await http.get(
                f"{URL}/sub_test/events?after_sequence_number=-1",
                headers={"Last-Event-ID": "0"},
            )
        )
        assert after == first[1:]
        assert (
            sse_payloads(
                await http.get(f"{URL}/sub_test/events?after_sequence_number=1")
            )
            == []
        )
        lookup = (await http.get(f"{URL}/sub_test")).json()
        assert (
            lookup["run"]["status"]
            == {"completed": "succeeded", "failed": "failed", "cancelled": "cancelled"}[
                status
            ]
        )
        assert lookup["run"]["last_sequence_number"] == 1
        assert (await http.post(URL, json=PAYLOAD)).json() == lookup
        assert len(starts) == 1


async def test_completed_row_without_terminal_event_is_not_success(
    tmp_path, starts, monkeypatch,
):
    class ConnectedRequest:
        async def is_disconnected(self):
            return False

    monkeypatch.setenv("QWENPAW_DATA_STREAM_SSE_HEARTBEAT_SECONDS", "0.01")
    async with service(tmp_path) as (http, state):
        run = (await http.post(URL, json=PAYLOAD)).json()["run"]
        chat = await state.chats.get(run["run_id"])
        chat.mark_status("completed")
        await state.chats.save(chat)
        assert (await http.get(f"{URL}/sub_test")).json()["run"][
            "status"
        ] == "reconciling"
        # A saved terminal status can precede the final event. Keep streaming
        # until that event arrives, or the client disconnects before restart.
        response = await routes.submission_events(
            ConnectedRequest(),
            "sub_test",
            Identity.anonymous(),
            state,
            -1,
            None,
        )
        iterator = response.body_iterator
        try:
            assert await asyncio.wait_for(anext(iterator), 5) == ": keepalive\n\n"
        finally:
            await iterator.aclose()
    async with service(tmp_path) as (http, state):
        lookup = (await http.get(f"{URL}/sub_test")).json()
        assert lookup["run"]["status"] == "interrupted"
        assert lookup["run"]["error"]["details"]["previous_status"] == "completed"
        assert (
            sse_payloads(await http.get(f"{URL}/sub_test/events"))[0]["status"]
            == "cancelled"
        )


@pytest.mark.parametrize(
    "chat_status,event_status",
    [
        ("failed", "failed"),
        ("canceled", "cancelled"),
    ],
)
async def test_restart_repairs_missing_failure_or_cancel_event(
    tmp_path,
    starts,
    chat_status,
    event_status,
):
    async with service(tmp_path) as (http, state):
        run = (await http.post(URL, json=PAYLOAD)).json()["run"]
        chat = await state.chats.get(run["run_id"])
        chat.mark_status(chat_status)
        chat.error = {"code": "VALIDATION", "message": "original error"}
        await state.chats.save(chat)
    async with service(tmp_path) as (http, _state):
        event = sse_payloads(await http.get(f"{URL}/sub_test/events"))[0]
        assert event["status"] == event_status
        assert event["error"]["message"] == "original error"
        assert (await http.get(f"{URL}/sub_test")).json()["run"][
            "status"
        ] == event_status


async def test_runtime_preparation_failure_has_queryable_terminal_result(
    tmp_path, monkeypatch
):
    # Exercise the real ChatRuntime.run/_finish path without accessing providers.
    async with service(tmp_path) as (http, state):

        def not_ready(**kwargs):
            raise ValueError("model is not configured")

        monkeypatch.setattr(state.hosts, "get", not_ready)
        accepted = await http.post(URL, json=PAYLOAD)
        assert accepted.status_code == 202
        await asyncio.gather(*list(state.tasks))
        lookup = (await http.get(f"{URL}/sub_test")).json()
        assert lookup["run"]["status"] == "failed"
        assert lookup["run"]["error"]["message"] == "model is not configured"
        events = sse_payloads(await http.get(f"{URL}/sub_test/events"))
        assert len(events) == 1
        assert events[0]["status"] == "failed"


async def test_deleted_run_never_becomes_not_found_or_resubmitted(tmp_path, starts):
    async with service(tmp_path) as (http, state):
        run = (await http.post(URL, json=PAYLOAD)).json()["run"]
        await finish(state, run)
        assert (
            await http.delete(f"/api/v1/sessions/{run['session_id']}")
        ).status_code == 204
        assert (await http.post(URL, json=PAYLOAD)).json()["run"]["run_id"] == run[
            "run_id"
        ]
        async with state.submissions._sessions() as db, db.begin():
            await db.execute(delete(ChatRow).where(ChatRow.id == run["run_id"]))
        lookup = (await http.get(f"{URL}/sub_test")).json()
        assert lookup["state"] == "accepted"
        assert lookup["run"]["status"] == "unknown"
        assert lookup["run"]["reason"] == "run_missing"
        assert (await http.post(URL, json=PAYLOAD)).json() == lookup
        assert (await http.get(f"{URL}/sub_test/events")).status_code == 410
        assert len(starts) == 1


async def test_database_failure_is_not_authoritative_not_found(tmp_path, monkeypatch):
    async with service(tmp_path, raise_app_exceptions=False) as (http, state):

        async def fail(*args):
            raise OperationalError("SELECT", {}, Exception("database unavailable"))

        monkeypatch.setattr(state.submissions, "lookup", fail)
        assert (await http.get(f"{URL}/sub_test")).status_code == 500


async def test_json_store_explicitly_declines_protocol(tmp_path, monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_STORE", "json")
    async with service(tmp_path) as (http, _state):
        caps = (await http.get("/api/v1/capabilities/submissions")).json()
        assert caps == {
            "protocol_version": None,
            "durable_submissions": False,
            "event_replay": False,
            "durable_commands": False,
            "scoped_host_capabilities": False,
            "artifact_handoff": False,
            "analysis_experience": False,
            "session_submissions": False,
            "optional_session_datasource": False,
            "dataset_context": False,
            "runtime_model_bindings": False,
            "scoped_sessions": True,
        }
        for response in (
            await http.post(URL, json=PAYLOAD),
            await http.get(f"{URL}/sub_test"),
            await http.get(f"{URL}/sub_test/events"),
        ):
            assert response.status_code == 501
            assert (
                response.json()["details"]["reason"]
                == "durable_submissions_unsupported"
            )


async def test_submission_api_requires_service_bearer(tmp_path, starts, monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_API_TOKEN", "test-token")
    async with service(tmp_path) as (http, _state):
        assert (await http.post(URL, json=PAYLOAD)).status_code == 401
        assert (await http.get("/api/v1/capabilities/submissions")).status_code == 401
        assert (await http.get(f"{URL}/sub_test")).status_code == 401
        assert (await http.get(f"{URL}/sub_test/events")).status_code == 401
        headers = {"Authorization": "Bearer test-token"}
        assert (await http.post(URL, json=PAYLOAD, headers=headers)).status_code == 202
        assert (
            await http.get("/api/v1/capabilities/submissions", headers=headers)
        ).json()["protocol_version"] == 1


@pytest.mark.parametrize(
    "change",
    [
        {"submission_id": ""},
        {"submission_id": "bad/id"},
        {"text": "  "},
        {"datasource_id": " "},
        {"protocol_version": 2},
        {"extra": "ignored?"},
        {"agent_id": "../unsafe"},
    ],
)
async def test_invalid_requests_do_not_create_runs(tmp_path, starts, change):
    async with service(tmp_path) as (http, state):
        response = await http.post(URL, json={**PAYLOAD, **change})
        assert response.status_code in (400, 422)
        assert await counts(state) == [0, 0, 0]
        assert not starts


async def test_host_model_snapshot_is_transient_and_replay_keeps_original(
    tmp_path, monkeypatch
):
    snapshots = []

    async def run(self, chat_id, *, identity, runtime_config, context_model_header):
        snapshots.append((runtime_config, context_model_header))

    monkeypatch.setattr(ChatRuntime, "run", run)
    binding = {
        "provider_id": "openai",
        "model_id": "model-a",
        "api_key": "runtime-only-key",
        "base_url": "https://models.example/v1",
    }
    payload = {
        **PAYLOAD,
        "runtime_models": {
            "analysis": binding,
            "lightweight": binding,
            "databridge": binding,
        },
    }
    body = SubmitRunRequest(**payload)
    assert "runtime-only-key" not in repr(body)
    assert "runtime_models" not in body.model_dump()
    assert body.request_digest() == SubmitRunRequest(**PAYLOAD).request_digest()
    async with service(tmp_path) as (http, state):
        accepted = await http.post(URL, json=payload)
        assert accepted.status_code == 202
        await asyncio.sleep(0)
        payload["runtime_models"]["analysis"] = {
            **binding,
            "model_id": "model-b",
            "api_key": "rotated",
        }
        replay = await http.post(URL, json=payload)
        assert replay.json() == accepted.json()
        assert len(snapshots) == 1
        assert snapshots[0][0].default.model_id == "model-a"
        assert snapshots[0][0].default.api_key == "runtime-only-key"
        assert "runtime-only-key" not in repr(snapshots[0][0])
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert b"runtime-only-key" not in path.read_bytes(), str(path)


@pytest.mark.parametrize("datasource", [None, "", "  "])
def test_new_submissions_still_require_a_datasource(datasource):
    with pytest.raises(ValueError):
        SubmitRunRequest(**{**PAYLOAD, "datasource_id": datasource})


def test_optional_session_datasource_has_stable_digest():
    omitted = SubmitRunRequest(
        submission_id="s1", text="check model", session_id="ses1"
    )
    explicit = SubmitRunRequest(
        submission_id="s1", text="check model", session_id="ses1", datasource_id=None
    )
    assert omitted.request_digest() == explicit.request_digest()


async def test_dataset_context_is_durable_and_idempotent(tmp_path, starts):
    from qwenpaw_data.host.core.runtime.turn import TurnInput, compose_agent_input

    payload = {**PAYLOAD, "dataset_names": ["dws_gaap_di"]}
    async with service(tmp_path) as (http, state):
        response = await http.post(URL, json=payload)
        assert response.status_code == 202, response.text
        run = response.json()["run"]
        chat = await state.chats.get(run["run_id"])
        assert chat.dataset_names == ["dws_gaap_di"]
        assert chat.user_input == PAYLOAD["text"]
        turn = TurnInput.from_chat(chat)
        prompt = compose_agent_input(
            turn.user_input, [], dataset_names=turn.dataset_names
        )
        assert "dws_gaap_di" in prompt
        assert "does not grant access permissions" in prompt
        assert (await http.post(URL, json=payload)).json()["run"]["run_id"] == run[
            "run_id"
        ]
        changed = await http.post(URL, json={**payload, "dataset_names": ["other"]})
        assert changed.status_code == 409
    async with service(tmp_path) as (_, state):
        assert (await state.chats.get(run["run_id"])).dataset_names == ["dws_gaap_di"]


@pytest.mark.parametrize("names", [[" "], ["x" * 257], ["x"] * 65, [1]])
def test_dataset_context_rejects_invalid_names(names):
    with pytest.raises(ValueError):
        SubmitRunRequest(**PAYLOAD, dataset_names=names)


def test_dataset_context_empty_preserves_legacy_digest():
    legacy = dict(PAYLOAD)
    legacy.pop("submission_id")
    legacy.update(protocol_version=1, agent_id="default", capability_bridge=None)
    import hashlib

    digest = hashlib.sha256(
        json.dumps(
            legacy, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()
    assert SubmitRunRequest(**PAYLOAD).request_digest() == digest
