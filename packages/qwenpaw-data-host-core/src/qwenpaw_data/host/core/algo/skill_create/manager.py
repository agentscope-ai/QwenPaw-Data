# -*- coding: utf-8 -*-
"""Unattended skill-draft pipeline: digest → induct → stepwise author → validate."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from qwenpaw_data.host.core.algo.skill_create.assets import (
    checklist_for_prompt,
    evidence_guide_for_prompt,
    reference,
)
from qwenpaw_data.host.core.algo.skill_create.digest import (
    build_digest,
    chats_to_messages,
    initial_task_instruction,
)
from qwenpaw_data.host.core.algo.skill_create.files import (
    excerpt_for_path,
    flags_in_text,
    sidecar_files,
    skill_payload_files,
)
from qwenpaw_data.host.core.algo.skill_create.hub import SkillIndex, uniquify_name
from qwenpaw_data.host.core.algo.skill_create.llm import (
    AUTHOR_BUNDLE_TIMEOUT,
    AUTHOR_SKILL_MD_TIMEOUT,
    Decision,
    FileSpec,
    SkillWriter,
    normalize_file_plan,
    render_file_plan,
)
from qwenpaw_data.host.core.algo.skill_create.prompts import (
    author_bundle_prompt,
    author_skill_md_prompt,
    induct_prompt,
)
from qwenpaw_data.host.core.algo.skill_create.validate import (
    ValidationReport,
    validate_files,
)
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.skill_manager import SkillManager
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.repositories import (
    ChatRepository,
    SessionRepository,
    SkillDraftRepository,
)
from qwenpaw_data.host.core.settings import get_settings

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
PATH_IN_DETAIL = re.compile(
    r"\b(SKILL\.md|scripts/[A-Za-z0-9_./-]+\.py|references/[A-Za-z0-9_./-]+\.md)\b"
)

Checkpoint = Callable[[dict[str, str]], Awaitable[None]]


class ExportError(RuntimeError):
    """Deterministic digest/export failure; do not retry."""


class SkillCreateManager:
    """Background generate / revise for one skill draft."""

    def __init__(self, writer: SkillWriter | None = None) -> None:
        self._writer = writer

    @classmethod
    async def generate(
        cls,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        identity: Identity,
        session_id: str,
        chat_id: str,
        draft_id: str,
        writer: SkillWriter | None = None,
    ) -> None:
        await cls(writer)._generate(
            session_factory,
            identity=identity,
            session_id=session_id,
            chat_id=chat_id,
            draft_id=draft_id,
        )

    @classmethod
    async def revise(
        cls,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        identity: Identity,
        session_id: str,
        draft_id: str,
        text: str,
        writer: SkillWriter | None = None,
    ) -> None:
        await cls(writer)._revise(
            session_factory,
            identity=identity,
            session_id=session_id,
            draft_id=draft_id,
            text=text,
        )

    async def _generate(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        identity: Identity,
        session_id: str,
        chat_id: str,
        draft_id: str,
    ) -> None:
        attempt_errors: list[str] = []
        progress: dict[str, str] = {}
        try:
            digest, index, writer = await self._load_generate_context(
                session_factory,
                identity=identity,
                session_id=session_id,
                chat_id=chat_id,
            )
            progress["evidence/digest.md"] = digest.markdown
            progress["evidence/digest.json"] = json.dumps(
                digest.payload, ensure_ascii=False, indent=2
            )
            decision, card, induct_errors = await self._induct(
                writer,
                digest=digest.markdown,
                task_query=digest.task_instruction,
                index=index,
            )
            attempt_errors.extend(induct_errors)
            if decision is None or not card:
                raise RuntimeError(
                    "induct failed: "
                    + (induct_errors[-1] if induct_errors else "no decision")
                )
            name = uniquify_name(_kebab_name(decision.skill_name), index.names())
            decision.skill_name = name
            decision.files = normalize_file_plan(decision.files)
            progress["skill-card.md"] = card
            progress["decision.json"] = json.dumps(
                decision.to_payload(), ensure_ascii=False, indent=2
            )
            files, author_errors, warnings = await self._author_stepwise(
                writer,
                plan=decision.files,
                skill_card=card,
                skill_name=name,
                taken_names=index.names(),
                require_planned_refs=True,
                checkpoint=_make_checkpoint(
                    self,
                    session_factory,
                    identity=identity,
                    session_id=session_id,
                    draft_id=draft_id,
                    progress=progress,
                    status="generating",
                ),
            )
            attempt_errors.extend(author_errors)
            if files is None:
                raise RuntimeError("author failed after retries")
            merged = {**progress, **files}
            if warnings:
                merged["report.md"] = _report_md(warnings, title="Skill draft warnings")
            await self._save(
                session_factory,
                identity=identity,
                session_id=session_id,
                draft_id=draft_id,
                files=merged,
                status="ready",
            )
        except ExportError as exc:
            logger.warning("skill draft export failed draft=%s: %s", draft_id, exc)
            await self._fail(
                session_factory,
                identity=identity,
                session_id=session_id,
                draft_id=draft_id,
                files=progress,
                errors=[f"export/digest failed (not retried): {exc}"],
            )
        except Exception as exc:
            logger.warning(
                "skill draft generate failed draft=%s", draft_id, exc_info=True
            )
            extra = [f"{type(exc).__name__}: {exc}"]
            await self._fail(
                session_factory,
                identity=identity,
                session_id=session_id,
                draft_id=draft_id,
                files=progress,
                errors=attempt_errors + extra,
            )

    async def _revise(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        identity: Identity,
        session_id: str,
        draft_id: str,
        text: str,
    ) -> None:
        attempt_errors: list[str] = []
        progress: dict[str, str] = {}
        try:
            current, index, writer = await self._load_revise_context(
                session_factory,
                identity=identity,
                session_id=session_id,
                draft_id=draft_id,
            )
            if not str(current.get("SKILL.md") or "").strip():
                raise RuntimeError("SKILL.md is required")
            progress.update(sidecar_files(current))
            expected = _name_of(current)
            files, author_errors, warnings = await self._author_stepwise(
                writer,
                plan=_plan_from_payload(current),
                skill_card=current.get("skill-card.md") or "",
                skill_name=expected,
                taken_names=index.names() - {expected},
                require_planned_refs=False,
                user_text=text,
                digest=current.get("evidence/digest.md") or "",
                previous=skill_payload_files(current),
                checkpoint=_make_checkpoint(
                    self,
                    session_factory,
                    identity=identity,
                    session_id=session_id,
                    draft_id=draft_id,
                    progress=progress,
                    status="revising",
                ),
            )
            attempt_errors.extend(author_errors)
            if files is None:
                raise RuntimeError("revise failed after retries")
            merged = {**progress, **files}
            if warnings:
                merged["report.md"] = _report_md(warnings, title="Skill draft warnings")
            await self._save(
                session_factory,
                identity=identity,
                session_id=session_id,
                draft_id=draft_id,
                files=merged,
                status="ready",
            )
        except Exception as exc:
            logger.warning(
                "skill draft revise failed draft=%s", draft_id, exc_info=True
            )
            extra = [f"{type(exc).__name__}: {exc}"]
            await self._fail(
                session_factory,
                identity=identity,
                session_id=session_id,
                draft_id=draft_id,
                files=progress,
                errors=attempt_errors + extra,
            )

    async def _load_generate_context(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        identity: Identity,
        session_id: str,
        chat_id: str,
    ) -> tuple[Any, SkillIndex, SkillWriter]:
        async with session_factory() as db:
            session = await SessionRepository(db, identity).get(session_id)
            chats = ChatRepository(db, identity)
            source = await chats.get(chat_id, session_id=session.id)
            history = [
                item
                for item in await chats.list_for_session(session.id)
                if item.sequence <= source.sequence
            ]
            messages_by_id = {
                item.id: await chats.read_messages(item.id) for item in history
            }
            index = _index_for_session(_skills_for_session(db, session))
            writer = self._writer or await _default_writer(
                db, identity, session_id=session.id, chat_id=source.id
            )
        try:
            messages = chats_to_messages(history, messages_by_id)
            digest = build_digest(
                messages,
                task_instruction=initial_task_instruction(history),
            )
        except Exception as exc:
            raise ExportError(str(exc)) from exc
        return digest, index, writer

    async def _load_revise_context(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        identity: Identity,
        session_id: str,
        draft_id: str,
    ) -> tuple[dict[str, str], SkillIndex, SkillWriter]:
        async with session_factory() as db:
            session = await SessionRepository(db, identity).get(session_id)
            draft = await SkillDraftRepository(db, identity).get(
                draft_id, session_id=session.id
            )
            index = _index_for_session(_skills_for_session(db, session))
            writer = self._writer or await _default_writer(
                db,
                identity,
                session_id=session.id,
                chat_id=str(draft["source_chat_id"]),
            )
        return dict(draft.get("files") or {}), index, writer

    async def _induct(
        self,
        writer: SkillWriter,
        *,
        digest: str,
        task_query: str,
        index: SkillIndex,
    ) -> tuple[Decision | None, str, list[str]]:
        errors: list[str] = []
        last_notes = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            prompt = induct_prompt(
                capture_boundary=reference("capture-boundary"),
                evidence_guide=evidence_guide_for_prompt(),
                digest=digest,
                skill_inventory=index.inventory(),
                task_query=task_query,
                failure_notes=last_notes,
            )
            try:
                decision, card = await writer.induct(prompt)
            except Exception as exc:
                last_notes = f"{type(exc).__name__}: {exc}"
                errors.append(f"induct attempt {attempt}: {last_notes}")
                continue
            decision.files = normalize_file_plan(decision.files)
            problems = _decision_problems(decision, card) + _plan_problems(
                decision.files
            )
            if not problems:
                return decision, card, errors
            last_notes = "\n".join(problems)
            errors.append(f"induct attempt {attempt}: {last_notes}")
        return None, "", errors

    async def _author_stepwise(
        self,
        writer: SkillWriter,
        *,
        plan: list[FileSpec],
        skill_card: str,
        skill_name: str,
        taken_names: set[str],
        require_planned_refs: bool,
        checkpoint: Checkpoint,
        user_text: str = "",
        digest: str = "",
        previous: dict[str, str] | None = None,
    ) -> tuple[dict[str, str] | None, list[str], list[str]]:
        errors: list[str] = []
        warnings: list[str] = []
        files: dict[str, str] = {}
        plan = normalize_file_plan(plan)
        planned = {spec.path for spec in plan}

        skill_md, skill_errors = await self._author_skill_md(
            writer,
            plan=plan,
            skill_card=skill_card,
            skill_name=skill_name,
            taken_names=taken_names,
            planned_paths=planned if require_planned_refs else None,
            user_text=user_text,
            digest=digest,
            previous=(previous or {}).get("SKILL.md", ""),
        )
        errors.extend(skill_errors)
        if skill_md is None:
            return None, errors, warnings
        name = _name_of({"SKILL.md": skill_md}) or skill_name
        if name in taken_names:
            name = uniquify_name(name, taken_names)
        skill_md = _ensure_frontmatter_name(skill_md, name)
        skill_name = name
        files["SKILL.md"] = skill_md
        await checkpoint(files)

        if not require_planned_refs:
            plan = [
                spec
                for spec in plan
                if spec.path == "SKILL.md" or PurePosixPath(spec.path).name in skill_md
            ]
            planned = {spec.path for spec in plan}

        for spec in list(plan):
            if spec.path == "SKILL.md":
                continue
            content, file_errors = await self._author_bundle(
                writer,
                spec=spec,
                files=files,
                skill_card=skill_card,
                skill_name=skill_name,
                taken_names=taken_names,
                planned_paths=planned,
                previous=(previous or {}).get(spec.path, ""),
            )
            errors.extend(file_errors)
            if content is not None:
                files[spec.path] = content
                await checkpoint(files)
                continue
            files.pop(spec.path, None)
            planned.discard(spec.path)
            warnings.append(f"dropped {spec.path} after retries")
            synced, sync_errors = await self._sync_skill_md(
                writer,
                files=files,
                skill_card=skill_card,
                skill_name=skill_name,
                taken_names=taken_names,
                planned_paths=planned,
                dropped_path=spec.path,
            )
            errors.extend(sync_errors)
            if synced is None:
                return None, errors, warnings
            files["SKILL.md"] = synced
            await checkpoint(files)

        repaired, repair_errors = await self._repair_bundle(
            writer,
            files=files,
            plan=[spec for spec in plan if spec.path in planned],
            skill_card=skill_card,
            skill_name=skill_name,
            taken_names=taken_names,
        )
        errors.extend(repair_errors)
        if repaired is None:
            return None, errors, warnings
        return repaired, errors, warnings

    async def _author_skill_md(
        self,
        writer: SkillWriter,
        *,
        plan: list[FileSpec],
        skill_card: str,
        skill_name: str,
        taken_names: set[str],
        planned_paths: set[str] | None,
        user_text: str,
        digest: str,
        previous: str,
    ) -> tuple[str | None, list[str]]:
        errors: list[str] = []
        last_notes = ""
        last_text = previous
        for attempt in range(1, MAX_ATTEMPTS + 1):
            prompt = author_skill_md_prompt(
                skill_card=skill_card,
                skill_name=skill_name,
                file_plan=render_file_plan(plan),
                previous_files=last_text,
                failure_notes=last_notes,
                user_text=user_text,
                digest=digest,
                checklist=checklist_for_prompt(),
                effective_guide=reference("effective-skill"),
            )
            try:
                authored = await writer.author_file(
                    prompt,
                    expected_path="SKILL.md",
                    timeout=AUTHOR_SKILL_MD_TIMEOUT,
                )
            except Exception as exc:
                last_notes = f"{type(exc).__name__}: {exc}"
                errors.append(f"author SKILL.md attempt {attempt}: {last_notes}")
                continue
            text = _single_content(authored, "SKILL.md")
            text = _ensure_frontmatter_name(text, skill_name)
            last_text = text
            report = validate_files(
                {"SKILL.md": text},
                taken_names=taken_names,
                expected_name=skill_name,
                planned_paths=planned_paths,
            )
            if report.ok:
                return text, errors
            last_notes = report.error_text() or "validation failed"
            errors.append(f"author SKILL.md attempt {attempt}: {last_notes}")
        return None, errors

    async def _author_bundle(
        self,
        writer: SkillWriter,
        *,
        spec: FileSpec,
        files: dict[str, str],
        skill_card: str,
        skill_name: str,
        taken_names: set[str],
        planned_paths: set[str],
        previous: str,
    ) -> tuple[str | None, list[str]]:
        errors: list[str] = []
        last_notes = ""
        last_text = previous
        excerpt = excerpt_for_path(files.get("SKILL.md") or "", spec.path)
        contract = excerpt or spec.contract
        for attempt in range(1, MAX_ATTEMPTS + 1):
            prompt = author_bundle_prompt(
                expected_path=spec.path,
                purpose=spec.purpose,
                contract=contract,
                skill_excerpt=excerpt,
                previous_content=last_text,
                failure_notes=last_notes,
                checklist=checklist_for_prompt(),
                effective_guide=reference("effective-skill"),
            )
            try:
                authored = await writer.author_file(
                    prompt,
                    expected_path=spec.path,
                    timeout=AUTHOR_BUNDLE_TIMEOUT,
                )
            except Exception as exc:
                last_notes = f"{type(exc).__name__}: {exc}"
                errors.append(f"author {spec.path} attempt {attempt}: {last_notes}")
                continue
            content = _single_content(authored, spec.path)
            last_text = content
            report = validate_files(
                {"SKILL.md": files["SKILL.md"], spec.path: content},
                taken_names=taken_names,
                expected_name=skill_name,
                planned_paths=planned_paths,
            )
            if report.ok:
                return content, errors
            last_notes = report.error_text() or "validation failed"
            errors.append(f"author {spec.path} attempt {attempt}: {last_notes}")
        return None, errors

    async def _sync_skill_md(
        self,
        writer: SkillWriter,
        *,
        files: dict[str, str],
        skill_card: str,
        skill_name: str,
        taken_names: set[str],
        planned_paths: set[str],
        dropped_path: str,
    ) -> tuple[str | None, list[str]]:
        remaining = [
            FileSpec(path=path, purpose="")
            for path in [
                "SKILL.md",
                *sorted(p for p in planned_paths if p != "SKILL.md"),
            ]
        ]
        notes = (
            f"已丢弃捆绑文件 `{dropped_path}`。从 SKILL.md 删除对它的引用、调用命令和参数表，"
            "不要提不存在的脚本或文档。"
        )
        text, errors = await self._author_skill_md(
            writer,
            plan=normalize_file_plan(remaining),
            skill_card=skill_card,
            skill_name=skill_name,
            taken_names=taken_names,
            planned_paths=planned_paths,
            user_text="",
            digest="",
            previous=files.get("SKILL.md", ""),
        )
        if text is None:
            return None, errors + [
                f"sync SKILL.md after dropping {dropped_path}: {notes}"
            ]
        report = validate_files(
            {"SKILL.md": text},
            taken_names=taken_names,
            expected_name=skill_name,
            planned_paths=planned_paths,
        )
        if not report.ok:
            return None, errors + [
                f"sync SKILL.md after dropping {dropped_path}: {report.error_text()}"
            ]
        return text, errors

    async def _repair_bundle(
        self,
        writer: SkillWriter,
        *,
        files: dict[str, str],
        plan: list[FileSpec],
        skill_card: str,
        skill_name: str,
        taken_names: set[str],
    ) -> tuple[dict[str, str] | None, list[str]]:
        errors: list[str] = []
        planned = {spec.path for spec in plan}
        payload = {
            path: files[path] for path in files if path in planned or path == "SKILL.md"
        }
        last_notes = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            report = validate_files(
                payload,
                taken_names=taken_names,
                expected_name=skill_name,
                planned_paths=planned,
            )
            if report.ok:
                return payload, errors
            last_notes = report.error_text() or "validation failed"
            errors.append(f"bundle repair attempt {attempt}: {last_notes}")
            targets = _targets_for_report(report)
            for path in targets:
                if path == "SKILL.md":
                    text, skill_errors = await self._author_skill_md(
                        writer,
                        plan=plan,
                        skill_card=skill_card,
                        skill_name=skill_name,
                        taken_names=taken_names,
                        planned_paths=planned,
                        user_text="",
                        digest="",
                        previous=payload.get("SKILL.md", ""),
                    )
                    errors.extend(skill_errors)
                    if text is None:
                        return None, errors
                    payload["SKILL.md"] = text
                    continue
                spec = next((item for item in plan if item.path == path), None)
                if spec is None:
                    payload.pop(path, None)
                    continue
                content, file_errors = await self._author_bundle(
                    writer,
                    spec=spec,
                    files=payload,
                    skill_card=skill_card,
                    skill_name=skill_name,
                    taken_names=taken_names,
                    planned_paths=planned,
                    previous=payload.get(path, ""),
                )
                errors.extend(file_errors)
                if content is None:
                    return None, errors
                payload[path] = content
        report = validate_files(
            payload,
            taken_names=taken_names,
            expected_name=skill_name,
            planned_paths=planned,
        )
        if report.ok:
            return payload, errors
        errors.append(f"bundle repair failed: {report.error_text()}")
        return None, errors

    async def _save(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        identity: Identity,
        session_id: str,
        draft_id: str,
        files: dict[str, str],
        status: str,
    ) -> None:
        async with session_factory() as db:
            await SkillDraftRepository(db, identity).save_files(
                draft_id,
                session_id=session_id,
                files=files,
                status=status,  # type: ignore[arg-type]
            )
            await db.commit()

    async def _fail(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        identity: Identity,
        session_id: str,
        draft_id: str,
        files: dict[str, str],
        errors: list[str],
    ) -> None:
        report = _report_md(errors)
        payload = {**files, "report.md": report}
        try:
            await self._save(
                session_factory,
                identity=identity,
                session_id=session_id,
                draft_id=draft_id,
                files=payload,
                status="failed",
            )
        except Exception:
            logger.exception("skill draft failed to persist failure draft=%s", draft_id)


def _make_checkpoint(
    manager: SkillCreateManager,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    identity: Identity,
    session_id: str,
    draft_id: str,
    progress: dict[str, str],
    status: str,
) -> Checkpoint:
    async def _checkpoint(payload: dict[str, str]) -> None:
        stale = [
            key
            for key in list(progress)
            if key not in payload and key.startswith(("scripts/", "references/"))
        ]
        for key in stale:
            progress.pop(key, None)
        progress.update(payload)
        await manager._save(
            session_factory,
            identity=identity,
            session_id=session_id,
            draft_id=draft_id,
            files=dict(progress),
            status=status,
        )

    return _checkpoint


def _skills_for_session(db: AsyncSession, session: Any) -> SkillManager:
    paths = UserAgentPaths.from_identity(
        home=get_settings().paths.home,
        identity=session.identity,
        agent_id=session.agent_id,
        session_id=session.id,
    )
    return SkillManager(db, identity=session.identity, paths=paths)


def _index_for_session(manager: SkillManager) -> SkillIndex:
    # 走文件层:生成技能只需要看已装了什么,不关心 Host 侧的启用开关。
    return SkillIndex.from_agent(agent_skills=manager.store.list_skills())


async def _default_writer(
    db: AsyncSession,
    identity: Identity,
    *,
    session_id: str,
    chat_id: str,
) -> SkillWriter:
    from qwenpaw_data.host.core.algo.settlement.llm import for_structured_calls
    from qwenpaw_data.host.core.algo.skill_create.llm import StructuredSkillWriter
    from qwenpaw_data.host.core.domain.model_usage import (
        ModelUsageContext,
        ModelUsageSource,
    )
    from qwenpaw_data.host.core.providers.factory import build_model
    from qwenpaw_data.host.core.repositories import PreferencesRepository

    prefs = await PreferencesRepository(db, identity).load()
    config = prefs.resolve()
    active = config.light or config.default
    model = for_structured_calls(
        build_model(
            active,
            usage=ModelUsageContext(
                ModelUsageSource.SKILL_CREATE, identity, session_id, chat_id
            ),
        )
    )
    return StructuredSkillWriter(model)


def _kebab_name(raw: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", (raw or "").strip().lower())
    return text.strip("-")[:64] or "generated-skill"


def _decision_problems(decision: Decision, card: str) -> list[str]:
    problems: list[str] = []
    if not card.strip():
        problems.append("skill_card is empty")
    if not _kebab_name(decision.skill_name):
        problems.append("skill_name is missing")
    elif not NAME_RE.match(_kebab_name(decision.skill_name)):
        problems.append(f"skill_name is not kebab-case: {decision.skill_name!r}")
    return problems


def _plan_problems(plan: list[FileSpec]) -> list[str]:
    if not plan or plan[0].path != "SKILL.md":
        return ["file plan must start with SKILL.md"]
    return []


def _plan_from_payload(files: dict[str, str]) -> list[FileSpec]:
    payload = skill_payload_files(files)
    skill_md = payload.get("SKILL.md") or ""
    specs = [FileSpec(path="SKILL.md", purpose="技能正文")]
    for path in payload:
        if path == "SKILL.md":
            continue
        excerpt = excerpt_for_path(skill_md, path)
        flags = " ".join(flags_in_text(excerpt))
        specs.append(FileSpec(path=path, purpose="", contract=flags or "--help"))
    return normalize_file_plan(specs)


def _name_of(files: dict[str, str]) -> str:
    from qwenpaw_data.host.core.utils.skill import parse_skill_frontmatter_text

    content = files.get("SKILL.md")
    if not content:
        return ""
    try:
        raw = parse_skill_frontmatter_text(content, source="SKILL.md").get("name")
    except ValueError:
        return ""
    return raw.strip() if isinstance(raw, str) else ""


def _ensure_frontmatter_name(content: str, name: str) -> str:
    match = re.match(r"\A---\s*\n(.*?)\n---\s*", content, re.DOTALL)
    if match is None:
        return f"---\nname: {name}\ndescription: 由证据摘要归纳的可复用分析流程。\n---\n\n{content}"
    body = content[match.end() :]
    raw = match.group(1)
    if re.search(r"(?m)^name:\s*", raw):
        raw = re.sub(r"(?m)^name:\s*.*$", f"name: {name}", raw, count=1)
    else:
        raw = f"name: {name}\n{raw}"
    return f"---\n{raw}\n---\n{body}"


def _single_content(files: dict[str, str], expected_path: str) -> str:
    if expected_path in files and isinstance(files[expected_path], str):
        return files[expected_path]
    if len(files) == 1:
        value = next(iter(files.values()))
        return value if isinstance(value, str) else ""
    return ""


def _targets_for_report(report: ValidationReport) -> list[str]:
    found: list[str] = []
    for check in report.errors:
        found.extend(PATH_IN_DETAIL.findall(check.detail))
        if not PATH_IN_DETAIL.search(check.detail):
            found.append("SKILL.md")
    ordered: list[str] = []
    for path in found:
        if path not in ordered:
            ordered.append(path)
    return ordered or ["SKILL.md"]


def _report_md(errors: list[str], *, title: str = "Skill draft failed") -> str:
    lines = [f"# {title}", ""]
    if not errors:
        lines.append("Unknown error.")
        return "\n".join(lines)
    for index, item in enumerate(errors, 1):
        lines.append(f"## Attempt {index}")
        lines.append("")
        lines.append(item)
        lines.append("")
    return "\n".join(lines)
