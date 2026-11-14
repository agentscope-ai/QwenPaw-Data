# -*- coding: utf-8 -*-
from __future__ import annotations

import json

import jsonschema
import pytest
from qwenpaw_data.host.core.agent.tools.ask_user_question import AskUserQuestionTool
from qwenpaw_data.host.core.domain.clarification import (
    ABSENT_USER_CLARIFICATION_TEXT,
    automatic_clarification_result,
)


def test_agent_input_schema_accepts_section_5_shape() -> None:
    payload = {
        "title": "请确认分析路径",
        "questions": [
            {
                "question": "你希望用哪个指标衡量访问情况？",
                "description": "分析口径会直接影响结论。",
                "multiSelect": False,
                "options": [
                    {"label": "全站 DAU", "description": "适合整体趋势分析"},
                    {"label": "分享页访问用户数", "description": None},
                ],
            },
            {
                "question": "你希望包含哪些渠道？",
                "description": "",
                "multiSelect": True,
                "options": [
                    {"label": "App"},
                    {"label": "Web"},
                    {"label": "MiniApp"},
                ],
            },
        ],
    }
    jsonschema.validate(payload, AskUserQuestionTool.build_input_schema())


def test_agent_input_schema_does_not_limit_item_counts() -> None:
    schema = AskUserQuestionTool.build_input_schema()
    questions = schema["properties"]["questions"]
    question = schema["$defs"]["_AskUserQuestionQuestion"]
    options = question["properties"]["options"]
    assert "minItems" not in questions
    assert "maxItems" not in questions
    assert "minItems" not in options
    assert "maxItems" not in options

    jsonschema.validate(
        {
            "title": "t",
            "questions": [
                {
                    "question": "q",
                    "multiSelect": False,
                    "options": [{"label": str(index)} for index in range(5)],
                }
            ],
        },
        schema,
    )


def test_agent_input_schema_rejects_blank_semantic_fields() -> None:
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {
                "title": " ",
                "questions": [
                    {
                        "question": "q",
                        "multiSelect": False,
                        "options": [{"label": "A"}, {"label": "B"}],
                    }
                ],
            },
            AskUserQuestionTool.build_input_schema(),
        )


def test_agent_input_schema_reads_soft_bounds_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qwenpaw_data.host.core.settings import ClarificationSettings, Settings

    settings = Settings(
        clarification=ClarificationSettings(
            questions_min_items=2,
            questions_max_items=3,
            options_min_items=3,
            options_max_items=5,
        )
    )
    monkeypatch.setattr(
        "qwenpaw_data.host.core.agent.tools.ask_user_question.get_settings",
        lambda: settings,
    )
    schema = AskUserQuestionTool.build_input_schema()
    questions = schema["properties"]["questions"]
    question = schema["$defs"]["_AskUserQuestionQuestion"]
    options = question["properties"]["options"]
    assert "prefer 2..3 items" in questions["description"]
    assert "Prefer 3..5 items" in options["description"]
    assert "minItems" not in questions
    assert "maxItems" not in questions
    assert "minItems" not in options
    assert "maxItems" not in options


def test_ask_user_question_tool_is_external() -> None:
    tool = AskUserQuestionTool()
    assert tool.is_external_tool is True
    assert tool.name == "ask_user_question"
    assert "multiSelect" in json.dumps(tool.input_schema)
    assert "question_id" not in json.dumps(tool.input_schema)
    assert "expires_at" not in json.dumps(tool.input_schema)


def test_clarification_answer_request_uses_semantic_answers() -> None:
    from qwenpaw_data.host.core.api.models.chat import AskUserQuestionAnsweredResultSchema
    from qwenpaw_data.host.core.api.models.requests import ClarificationAnswerRequest

    request = ClarificationAnswerRequest.model_validate(
        {
            "clarification_id": "call_1",
            "result": {
                "status": "answered",
                "answers": [
                    {
                        "question": "渠道？",
                        "selected_options": ["App", "Web"],
                        "custom_text": "只看自然流量",
                    },
                    {
                        "question": "其他要求？",
                        "selected_options": [],
                        "custom_text": "排除投放",
                    },
                ],
            },
        }
    )

    assert request.result.status == "answered"
    assert isinstance(request.result, AskUserQuestionAnsweredResultSchema)
    assert request.result.answers[0].selected_options == ["App", "Web"]


def test_automatic_clarification_covers_every_question() -> None:
    result = automatic_clarification_result(
        {
            "title": "Confirm",
            "questions": [
                {"question": "Color?"},
                {"question": "Unit?"},
            ],
        }
    )
    assert result == {
        "status": "answered",
        "answers": [
            {
                "question": "Color?",
                "selected_options": [],
                "custom_text": ABSENT_USER_CLARIFICATION_TEXT,
            },
            {
                "question": "Unit?",
                "selected_options": [],
                "custom_text": ABSENT_USER_CLARIFICATION_TEXT,
            },
        ],
    }
