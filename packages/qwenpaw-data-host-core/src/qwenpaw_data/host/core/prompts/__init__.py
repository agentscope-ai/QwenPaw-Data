"""Prompt template loading for QwenPaw Data."""

from __future__ import annotations

from pathlib import Path

from ..constants import is_spawn_subagent_enabled

SUBAGENT_SECTION_BEGIN = "<!-- QWENPAW_DATA_SUBAGENT_BEGIN -->"
SUBAGENT_SECTION_END = "<!-- QWENPAW_DATA_SUBAGENT_END -->"


def _strip_subagent_markers(content: str) -> str:
    content = content.replace(f"{SUBAGENT_SECTION_BEGIN}\n", "")
    content = content.replace(f"{SUBAGENT_SECTION_END}\n", "")
    content = content.replace(SUBAGENT_SECTION_BEGIN, "")
    return content.replace(SUBAGENT_SECTION_END, "")


def _remove_subagent_section(content: str) -> str:
    start = content.find(SUBAGENT_SECTION_BEGIN)
    if start < 0:
        return content
    stop = content.find(
        SUBAGENT_SECTION_END,
        start + len(SUBAGENT_SECTION_BEGIN),
    )
    if stop < 0:
        return _strip_subagent_markers(content)
    stop += len(SUBAGENT_SECTION_END)
    before = content[:start].rstrip()
    after = content[stop:].lstrip()
    if before and after:
        return f"{before}\n\n{after}"
    return f"{before}{after}"


def _render_prompt_file(content: str, *, filename: str, mode: str) -> str:
    if filename != "MASTER.md":
        return content
    if mode == "agent" and is_spawn_subagent_enabled():
        return _strip_subagent_markers(content)
    return _remove_subagent_section(content)


def build_master_prompt(
    *,
    mode: str = "agent",
    prompt_dir: Path | str | None = None,
) -> str:
    """Build the QwenPaw Data system prompt from markdown templates."""
    root = Path(prompt_dir) if prompt_dir is not None else Path(__file__).resolve().parent
    parts: list[str] = []

    for filename in (
        "MASTER.md",
        "PLAN_MODE.md" if mode == "plan" else "AGENT_MODE.md",
        "PLANNER.md",
    ):
        path = root / filename
        if path.exists():
            content = path.read_text(encoding="utf-8")
            parts.append(_render_prompt_file(content, filename=filename, mode=mode))

    return "\n\n".join(parts) if parts else "You are QwenPaw Data MasterAgent."


def analysis_environment_hint(
    *,
    session_id: str,
    prompt_dir: Path | str | None = None,
) -> str:
    """Return the runtime analysis-environment block appended to the system prompt."""
    root = Path(prompt_dir) if prompt_dir is not None else Path(__file__).resolve().parent
    path = root / "ANALYSIS_ENVIRONMENT.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip().format(session_id=session_id)
