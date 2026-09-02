# -*- coding: utf-8 -*-
"""Feishu interactive-card templates."""
from __future__ import annotations

from typing import Any, Optional

from qwenpaw_data.host.core.channels.clarification_question import (
    ClarificationQuestionGroup,
)
from qwenpaw_data.host.core.channels.segment_markup import render_segment_spans


def resolved_question_keys(
    group: ClarificationQuestionGroup,
    selections: dict[str, list[str]],
    resolved_multi: set[str],
) -> set[str]:
    """Return answered question keys: single-select on first click (non-empty
    selection), multi-select once its confirm button was pressed (in ``resolved_multi``).
    """
    resolved: set[str] = set()
    for i, question in enumerate(group.questions):
        qk = str(i)
        if question.multi_select:
            if qk in resolved_multi:
                resolved.add(qk)
        elif selections.get(qk):
            resolved.add(qk)
    return resolved


def build_clarification_card(
    card_id: str,
    group: ClarificationQuestionGroup,
    selections: dict[str, list[str]],
    resolved_multi: set[str] = frozenset(),
    *,
    warn: str | None = None,
    submitted: bool = False,
) -> dict[str, Any]:
    """Build a feishu v1 card rendering an ask_user_question group.

    One option-button row per question; single-select resolves on click,
    multi-select via its own confirm button; no whole-card submit — the group
    auto-submits once all resolve. ``submitted=True`` keeps everything visible
    (chosen options ✓) but disables all buttons and shows a green banner.
    """
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": (
                "✅ 你的选择已提交，正在继续执行任务。"
                if submitted
                else "请点选下列选项；单选点选即答，多选选择后点「完成此题」。"
            ),
        }
    ]
    for i, question in enumerate(group.questions):
        qk = str(i)
        current = selections.get(qk, []) if selections else []
        lines = [f"**Q{i + 1}. {question.question}**"]
        if question.required:
            lines[-1] += " *"
        if question.multi_select:
            lines[-1] += "（可多选）"
        if qk in resolved_multi:
            lines[-1] += "（已确认）"
        if question.description:
            lines.append(f"_{question.description}_")
        if current:
            lines.append(f"已选：{'、'.join(current)}")
        elements.append(
            {
                "tag": "markdown",
                "content": "\n".join(lines),
            }
        )

        buttons = []
        for answer in question.options or []:
            label = str(answer.label or "")
            if not label:
                continue
            checked = label in current

            desc = str(answer.description or "").strip()
            display = f"{label}（{desc}）" if desc else label
            btn = {
                "tag": "button",
                "text": {
                    "tag": "plain_text",
                    "content": f"✓ {display}" if checked else display,
                },
                "type": "primary" if checked else "default",
                "value": {
                    "type": "clarification_opt",
                    "card_id": card_id,
                    "qk": qk,
                    "opt": label,
                },
            }
            if submitted:
                btn["disabled"] = True
            buttons.append(btn)
        if buttons:
            elements.append({"tag": "action", "actions": buttons})

        if question.multi_select:
            confirmed = qk in resolved_multi
            confirm_btn = {
                "tag": "button",
                "text": {
                    "tag": "plain_text",
                    "content": "✅ 已确认" if confirmed else "完成此题",
                },
                "type": "primary" if confirmed else "default",
                "value": {
                    "type": "clarification_confirm",
                    "card_id": card_id,
                    "qk": qk,
                },
            }
            if submitted:
                confirm_btn["disabled"] = True
            elements.append(
                {
                    "tag": "action",
                    "actions": [confirm_btn],
                }
            )
        elements.append({"tag": "hr"})

    if warn:
        elements.append(
            {
                "tag": "markdown",
                "content": f"⚠️ {warn}",
            }
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "green" if submitted else "blue",
            "title": {
                "tag": "plain_text",
                "content": (
                    f"✅ {group.title or '补充信息'}"
                    if submitted
                    else (group.title or "补充信息")
                ),
            },
        },
        "elements": elements,
    }


def build_clarification_done_card() -> dict[str, Any]:
    """Final state swapped into a clarification card once it is submitted."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "green",
            "title": {"tag": "plain_text", "content": "✅ 已收到你的回答"},
        },
        "elements": [
            {
                "tag": "markdown",
                "content": "你的选择已提交，正在继续执行任务。",
            }
        ],
    }


def build_followup_card(
    *,
    card_id: str,
    questions: list[str],
    receive_id: str,
    receive_id_type: str,
    chat_id: str,
    sender_id: str,
    clicked: str | None = None,
) -> dict[str, Any]:
    """Build a feishu v1 follow-up question card.

    ``clicked=None`` (initial): blue, all buttons clickable. ``clicked`` set
    (frozen): green header, clicked button ``primary``+``✓``, every button
    disabled — instant feedback, cannot be re-triggered.
    """
    submitted = clicked is not None
    buttons: list[dict[str, Any]] = []
    for q in questions:
        text = str(q or "").strip()
        if not text:
            continue
        is_clicked = submitted and text == clicked
        btn = {
            "tag": "button",
            "text": {
                "tag": "plain_text",
                "content": f"✓ {text[:100]}" if is_clicked else text[:100],
            },
            "type": "primary" if is_clicked else "default",
            "value": {
                "type": "followup_select",
                "card_id": card_id,
                "question": text,
                "receive_id": receive_id,
                "receive_id_type": receive_id_type,
                "chat_id": chat_id,
                "sender_id": sender_id,
            },
        }
        if submitted:
            btn["disabled"] = True
        buttons.append(btn)

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "green" if submitted else "blue",
            "title": {
                "tag": "plain_text",
                "content": "✅ 继续追问" if submitted else "继续追问",
            },
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    f"✅ 已追问：{clicked}" if submitted else "点击问题继续追问："
                ),
            },
            {"tag": "hr"},
            {"tag": "action", "actions": buttons},
        ],
    }


def build_datasource_card(
    *,
    card_id: str,
    items: list[tuple[str, str]],
    receive_id: str,
    receive_id_type: str,
    chat_id: str,
    sender_id: str,
    session_id: str,
    selectable: bool = True,
    bound_ds_id: str | None = None,
    clicked_ds_id: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Build a feishu v1 datasource selection card.

    - ``selectable=True, clicked_ds_id=None`` (initial): blue, all clickable.
    - ``selectable=True, clicked_ds_id=X`` (optimistic freeze): green, clicked
      button ``primary``+``✓``, all disabled.
    - ``selectable=False`` (view-only, bound): green, bound button ``primary``+``✓``,
      all disabled — no click chance.
    ``note`` overrides the body line. Each button ``value`` carries ``card_id``
    so the sync handler can look up the stored options to rebuild the frozen card.
    """
    frozen = clicked_ds_id is not None or not selectable
    highlight = clicked_ds_id or bound_ds_id

    buttons: list[dict[str, Any]] = []
    for ds_id, ds_name in items:
        is_hi = highlight is not None and ds_id == highlight
        btn = {
            "tag": "button",
            "text": {
                "tag": "plain_text",
                "content": f"✓ {ds_name}" if is_hi else ds_name,
            },
            "type": "primary" if is_hi else "default",
            "value": {
                "type": "datasource_select",
                "card_id": card_id,
                "ds_id": ds_id,
                "ds_name": ds_name,
                "session_id": session_id,
                "receive_id": receive_id,
                "receive_id_type": receive_id_type,
                "chat_id": chat_id,
                "sender_id": sender_id,
            },
        }
        if frozen:
            btn["disabled"] = True
        buttons.append(btn)

    if clicked_ds_id is not None:
        header_tpl, header_title, body = (
            "green", "✅ 已选择数据源", (note or "已选择，正在绑定…")
        )
    elif not selectable:
        header_tpl, header_title, body = (
            "green",
            "✅ 当前数据源（已绑定）",
            (note or "数据源一经绑定不可更改，如需切换请新建会话："),
        )
    else:
        header_tpl, header_title, body = (
            "blue",
            "选择数据源",
            (note or "请选择本次会话使用的数据源，选定后可直接提问："),
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": header_tpl,
            "title": {"tag": "plain_text", "content": header_title},
        },
        "elements": [
            {"tag": "markdown", "content": body},
            {"tag": "hr"},
            {"tag": "action", "actions": buttons},
        ],
    }


def clarification_response(card: dict[str, Any], toast: str) -> Any:
    """Wrap a card + toast into a ``P2CardActionTriggerResponse``. Returning
    this from the card-action callback makes Feishu instantly swap the source
    card and pop a toast — built-in feedback, no ``message_id`` round-trip.
    """
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        CallBackCard,
        CallBackToast,
        P2CardActionTriggerResponse,
    )

    resp = P2CardActionTriggerResponse({})
    resp.toast = CallBackToast({})
    resp.toast.type = "success"
    resp.toast.content = toast
    resp.card = CallBackCard({})
    resp.card.type = "raw"
    resp.card.data = card
    return resp


def build_segment_card(seg: Any) -> dict[str, Any] | None:
    """Build a feishu v1 interactive card for a BizTrace segment."""
    title = str(getattr(seg, "title", "") or "").strip()
    started = getattr(seg, "started_at", None)
    ended = getattr(seg, "ended_at", None)
    dur = ""
    if started and ended and ended > started:
        secs = int(round(ended - started))
        dur = f"用时 {secs // 60}分{secs % 60}秒" if secs >= 60 else f"用时 {secs}秒"

    sections: list[tuple[str, str, str]] = []  # (icon, label, body)
    for label, attr, icon in (
        ("输入", "input", "📥"),
        ("执行", "behavior", "⚙️"),
        ("结论", "conclusion", "✅"),
    ):
        body = str(getattr(seg, attr, None) or "").strip()
        if body:
            sections.append((icon, label, render_segment_spans(body, target="feishu")))

    artifact_block = _format_segment_artifacts(seg)
    if artifact_block:
        sections.append(("📎", "关键产物", artifact_block))

    if not title and not sections:
        return None
    header_title = f"{title}  {dur}" if (title and dur) else (title or "分析步骤")

    elements: list[dict[str, Any]] = []
    for i, (icon, label, body) in enumerate(sections):
        if i:
            elements.append({"tag": "hr"})
        elements.append({"tag": "markdown", "content": f"**{icon} {label}**\n{body}"})
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": header_title},
        },
        "elements": elements,
    }


def _format_segment_artifacts(seg: Any) -> str:
    """Render ``seg.artifact`` as a one-line-per-file markdown list. """
    items = getattr(seg, "artifact", None) or []
    lines: list[str] = []
    for item in items:
        name = str(getattr(item, "name", "") or "").strip()
        if not name:
            continue
        desc = str(getattr(item, "description", "") or "").strip()
        lines.append(f"- {name}" + (f" — {desc}" if desc else ""))
    return "\n".join(lines)
