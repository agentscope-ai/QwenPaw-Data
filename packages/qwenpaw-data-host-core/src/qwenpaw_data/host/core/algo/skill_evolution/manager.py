# -*- coding: utf-8 -*-
"""Read chat, call skill evolution, persist and apply one session proposal."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.algo.skill_evolution.apply import select_accepted_patches
from qwenpaw_data.host.core.algo.skill_evolution.merge import merge_pending_proposal
from qwenpaw_data.host.core.algo.skill_evolution.models import EvolutionProposal, SignalState
from qwenpaw_data.host.core.algo.skill_evolution.propose import propose_evolution
from qwenpaw_data.host.core.algo.skill_evolution.skills_io import (
    evolvable_catalog,
    load_catalog,
)
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.skill import SkillConflictError, SkillFile
from qwenpaw_data.host.core.domain.skill_manager import SkillManager
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.repositories import ChatRepository
from qwenpaw_data.host.core.repositories.skill_proposal import SkillProposalRepository
from qwenpaw_data.host.core.utils.skill import normalize_rel_path

_WINDOW = 8


def _message_text(msg: dict[str, Any]) -> str:
    parts = [
        str(block.get("text") or "")
        for block in msg.get("content") or []
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(part for part in parts if part).strip()


def _signal_from_messages(
    messages: list[dict[str, Any]],
) -> tuple[SignalState, list[str]]:
    count = 0
    texts: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if str(msg.get("status") or "completed") not in ("completed", "complete", ""):
            continue
        text = _message_text(msg)
        if not text:
            continue
        msg_type = str(msg.get("type") or "message")
        meta = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
        if msg_type == "message" and msg.get("role") == "user":
            count += 1
            texts.append(text)
        elif msg_type == "hint" and meta.get("source") == "steer":
            count += 1
            texts.append(text)
    return SignalState(user_feedback_count=count), texts


def _signal_from_chat(
    messages: list[dict[str, Any]],
    user_inputs: list[str],
) -> tuple[SignalState, list[str]]:
    """Count this chat's ``user_input`` even when it is not a chat-event."""
    signal, texts = _signal_from_messages(messages)
    seen = {item.strip() for item in texts}
    extras: list[str] = []
    for raw in user_inputs:
        text = (raw or "").strip()
        if not text or text in seen:
            continue
        extras.append(text)
        seen.add(text)
    return (
        SignalState(user_feedback_count=signal.user_feedback_count + len(extras)),
        extras + texts,
    )


def _group_by_skill(
    patches: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """One revision per skill, even when a proposal touches several files."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for patch in patches:
        grouped.setdefault(str(patch["skill_name"]), []).append(patch)
    return grouped


def _merge_after_accept(
    original: list[dict[str, Any]],
    selected: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    selected_by_id = {str(item.get("patch_id") or ""): item for item in selected}
    merged: list[dict[str, Any]] = []
    accepted = 0
    total = 0
    for patch in original:
        hunks = [hunk for hunk in patch.get("hunks") or [] if isinstance(hunk, dict)]
        total += len(hunks)
        chosen = selected_by_id.get(str(patch.get("patch_id") or ""))
        if chosen is None:
            merged.append(
                {
                    **patch,
                    "status": "rejected",
                    "hunks": [{**hunk, "status": "rejected"} for hunk in hunks],
                }
            )
            continue
        merged.append(chosen)
        accepted += sum(
            1 for hunk in chosen.get("hunks") or [] if hunk.get("status") == "accepted"
        )
    return merged, ("accepted" if total and accepted == total else "partial")


class SkillEvolutionManager:
    def __init__(
        self,
        db: AsyncSession,
        *,
        identity: Identity,
        paths: UserAgentPaths,
    ) -> None:
        self._identity = identity
        self._paths = paths
        self._repo = SkillProposalRepository(db, identity)
        self._chats = ChatRepository(db, identity)
        self._skills = SkillManager(db, identity=identity, paths=paths)

    @classmethod
    async def after_reply(
        cls,
        factory: Any,
        *,
        identity: Identity,
        paths: UserAgentPaths,
        chat_id: str,
        session_id: str,
    ) -> None:
        # 读库和写库各开短会话；中间 LLM 不能握着连接。
        async with factory() as db:
            kwargs = await cls(db, identity=identity, paths=paths).prepare_propose(
                chat_id=chat_id, session_id=session_id
            )
        proposal = await propose_evolution(**kwargs)
        if proposal is None:
            return
        async with factory() as db:
            await cls(db, identity=identity, paths=paths).persist_proposal(
                session_id=session_id, chat_id=chat_id, proposal=proposal
            )
            await db.commit()

    async def prepare_propose(self, *, chat_id: str, session_id: str) -> dict[str, Any]:
        from qwenpaw_data.host.core.domain.model_usage import (
            ModelUsageContext,
            ModelUsageSource,
        )
        from qwenpaw_data.host.core.providers.factory import build_model
        from qwenpaw_data.host.core.repositories import PreferencesRepository

        this_messages, this_inputs = await self._this_chat_messages(
            session_id=session_id, chat_id=chat_id
        )
        signal, feedback_texts = _signal_from_chat(this_messages, this_inputs)

        rows = await self._repo.list_by_session(session_id)
        prior_pending = await self._living_pending(session_id)
        incremental = bool(rows)
        if prior_pending is not None:
            messages, chat_user_inputs = this_messages, this_inputs
        elif incremental:
            messages, chat_user_inputs = this_messages, this_inputs
        else:
            messages, chat_user_inputs = await self._window_messages(
                session_id=session_id, chat_id=chat_id
            )

        prefs = await PreferencesRepository(self._repo.db, self._identity).load()
        return {
            "model": build_model(
                prefs.resolve().default,
                usage=ModelUsageContext(
                    ModelUsageSource.SKILL_EVOLUTION,
                    self._identity,
                    session_id,
                    chat_id,
                ),
            ),
            "catalog": evolvable_catalog(load_catalog(self._skills.store)),
            "messages": messages,
            "signal": signal,
            "feedback_texts": feedback_texts,
            "chat_user_inputs": chat_user_inputs,
            "incremental": incremental,
            "prior_pending": prior_pending,
        }

    async def persist_proposal(
        self, *, session_id: str, chat_id: str, proposal: EvolutionProposal
    ) -> None:
        merged = merge_pending_proposal(
            await self._living_pending(session_id),
            proposal,
        )
        await self._repo.upsert_pending(
            session_id=session_id,
            agent_id=self._paths.agent_id,
            summary=merged.summary,
            mentioned_skill_names=list(merged.skills),
            modified_file_count=merged.patch_count,
            file_changes=[dict(item) for item in merged.patches],
            source_chat_id=chat_id,
        )

    async def _living_pending(self, session_id: str) -> EvolutionProposal | None:
        rows = await self._repo.list_by_session(session_id)
        pending_row = next((row for row in rows if row["status"] == "pending"), None)
        if pending_row is None:
            return None
        detail = await self._repo.get(pending_row["id"], session_id=session_id)
        return EvolutionProposal(
            summary=str(detail.get("summary") or ""),
            skills=list(detail.get("mentioned_skill_names") or []),
            patch_count=int(detail.get("modified_file_count") or 0),
            patches=list(detail.get("file_changes") or []),
        )

    async def accept(
        self,
        proposal_id: str,
        *,
        session_id: str,
        patch_ids: list[str] | None = None,
        hunk_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        row = await self._repo.get(proposal_id, session_id=session_id)
        if row["status"] != "pending":
            raise ValueError(f"skill proposal is not pending: {proposal_id}")
        original = list(row["file_changes"] or [])
        selected = select_accepted_patches(original, patch_ids, hunk_ids)
        merged, status = _merge_after_accept(original, selected)
        for skill_name, patches in _group_by_skill(selected).items():
            if self._skills.store.info(skill_name).source == "builtin":
                raise PermissionError(f"cannot evolve builtin skill: {skill_name}")
            # Resolved containment (symlinks included) before any write; the
            # store's str-prefix check would admit sibling dirs like "<name>-x".
            root = self._skills.store.require(skill_name).resolve()
            for patch in patches:
                rel = normalize_rel_path(str(patch["relative_path"]))
                if not (root / rel).resolve().is_relative_to(root):
                    raise ValueError(f"path escape rejected: {rel}")
            try:
                await self._skills.apply_files(
                    skill_name,
                    [
                        SkillFile(
                            relative_path=str(patch["relative_path"]),
                            content=str(patch["after"]),
                        )
                        for patch in patches
                    ],
                    expect=[
                        SkillFile(
                            relative_path=str(patch["relative_path"]),
                            content=str(patch.get("before") or ""),
                        )
                        for patch in patches
                    ],
                    origin="evolution_accept",
                    source_ref=proposal_id,
                    note=str(row["summary"] or ""),
                )
            except SkillConflictError as exc:
                exc.reason = "SKILL_CHANGED"
                raise
        return await self._repo.save_accepted(
            proposal_id,
            session_id=session_id,
            status=status,  # type: ignore[arg-type]
            file_changes=merged,
        )

    async def _window_messages(
        self, *, session_id: str, chat_id: str
    ) -> tuple[list[dict[str, Any]], list[str]]:
        chats = await self._chats.list_for_session(session_id)
        window = [
            chat for chat in chats if chat.status == "completed" or chat.id == chat_id
        ][-_WINDOW:]
        messages: list[dict[str, Any]] = []
        chat_user_inputs: list[str] = []
        for chat in window:
            chat_user_inputs.append(chat.user_input)
            for msg in await self._chats.read_messages(chat.id):
                messages.append(
                    {
                        **msg,
                        "_chat_id": chat.id,
                        "_chat_user_input": chat.user_input,
                    }
                )
        return messages, chat_user_inputs

    async def _this_chat_messages(
        self, *, session_id: str, chat_id: str
    ) -> tuple[list[dict[str, Any]], list[str]]:
        try:
            chat = await self._chats.get(chat_id, session_id=session_id)
        except LookupError:
            return [], []
        messages: list[dict[str, Any]] = []
        for msg in await self._chats.read_messages(chat.id):
            messages.append(
                {
                    **msg,
                    "_chat_id": chat.id,
                    "_chat_user_input": chat.user_input,
                }
            )
        return messages, [chat.user_input]
