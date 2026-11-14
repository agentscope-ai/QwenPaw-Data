"""CLI renders current Host events without relaxing server-side permissions."""

from unittest.mock import AsyncMock

import pytest

from qwenpaw_data.cli.host import create_client, print_turn, read_terminal_input
from qwenpaw_data.cli.main import build_parser


class EventsClient:
    def __init__(self, events):
        self.events = events
        self.closed = False
        self.answer_tool_confirmation = AsyncMock()

    async def run(self, *args, **kwargs):
        try:
            for event in self.events:
                yield {"chat_id": "c1", **event}
        finally:
            self.closed = True


@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize("delta", ["hello", "hel"])
async def test_text_is_not_duplicated_and_tool_errors_remain_visible(
    stream, delta, capsys
):
    message = {"object": "message", "id": "m1", "role": "assistant", "type": "message"}
    client = EventsClient(
        [
            {**message, "status": "in_progress", "content": []},
            {
                "object": "content",
                "type": "text",
                "msg_id": "m1",
                "delta": True,
                "text": delta,
            },
            {
                "object": "content",
                "type": "text",
                "msg_id": "m1",
                "delta": False,
                "text": "hello",
            },
            {
                **message,
                "status": "completed",
                "content": [{"type": "text", "text": "hello"}],
            },
            {
                "object": "message",
                "type": "plugin_call_output",
                "status": "completed",
                "content": [
                    {
                        "type": "data",
                        "data": {"name": "Bash", "state": "error", "output": "failed"},
                    }
                ],
            },
        ]
    )
    await print_turn(client, "s1", "prompt", stream=stream)
    output = capsys.readouterr()
    assert output.out == "hello\n"
    assert "[tool error] Bash: failed" in output.err
    assert client.closed


@pytest.mark.parametrize(
    "interactive,answer,approved",
    [(False, None, False), (True, "yes", True), (True, "", False)],
)
async def test_confirmation_denies_non_tty_and_requires_explicit_yes(
    monkeypatch, interactive, answer, approved
):
    monkeypatch.setattr("sys.stdin.isatty", lambda: interactive)

    def prompt():
        assert interactive
        return answer

    monkeypatch.setattr("builtins.input", prompt)
    event = {
        "object": "message",
        "type": "plugin_call",
        "status": "completed",
        "source_id": "call1",
        "content": [],
        "metadata": {"awaiting_confirmation": True},
    }
    client = EventsClient([event, event])
    await print_turn(client, "s1", "prompt", stream=True)
    client.answer_tool_confirmation.assert_awaited_once_with(
        "s1", "c1", "call1", approved=approved
    )


def test_no_implicit_local_execution_or_databridge_token(monkeypatch):
    monkeypatch.delenv("QWENPAW_DATA_HOST_URL", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_HOST_TOKEN", raising=False)
    monkeypatch.setenv("QWENPAW_DATA_API_TOKEN", "databridge-only-token")
    args = build_parser().parse_args(["run", "hello"])
    with pytest.raises(ValueError, match="host-url"):
        create_client(args)


@pytest.mark.parametrize("interrupted", [False, True])
def test_terminal_read_restores_runner_signal_handler(monkeypatch, interrupted):
    import signal

    original = signal.getsignal(signal.SIGINT)

    def read():
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
        if interrupted:
            raise KeyboardInterrupt()
        return "hello"

    monkeypatch.setattr("builtins.input", read)
    if interrupted:
        with pytest.raises(KeyboardInterrupt):
            read_terminal_input()
    else:
        assert read_terminal_input() == "hello"
    assert signal.getsignal(signal.SIGINT) == original
