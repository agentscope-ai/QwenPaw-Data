"""Per-agent delegation roles. A role selects capabilities; it never grants them."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ..paths import UserAgentPaths
from ..utils.safe_name import require_safe_name
from ..utils.workspace_file import open_resolved_workspace_file

_PRESETS = Path(__file__).with_name("subagent_presets")
_MAX_BYTES = 64 * 1024
_FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.S)


class _Header(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    description: str = Field(min_length=1, max_length=1024)
    tools: list[str] | None = None
    skills: list[str] | None = None
    mcp: list[str] | None = None


@dataclass(frozen=True)
class SubagentRole:
    name: str
    description: str
    body: str
    tools: tuple[str, ...] | None = None
    skills: tuple[str, ...] | None = None
    mcp: tuple[str, ...] | None = None


def _parse(path: Path, data: bytes) -> SubagentRole:
    if len(data) > _MAX_BYTES:
        raise ValueError(f"subagent role too large: {path.name}")
    text = data.decode("utf-8").replace("\r\n", "\n")
    match = _FRONTMATTER.match(text)
    if not match:
        raise ValueError(f"missing subagent frontmatter: {path.name}")
    try:
        fields = yaml.safe_load(match[1])
        if not isinstance(fields, dict):
            raise ValueError("expected a mapping")
        header = _Header.model_validate({"name": path.stem, **fields})
        if not header.name or not header.description.strip():
            raise ValueError("empty role name or description")

        # Omitted lists inherit. An explicitly empty list means no access.
        def names(key: str) -> tuple[str, ...] | None:
            if key not in fields:
                return None
            values = getattr(header, key)
            if values is None or any(not v.strip() for v in values):
                raise ValueError(f"invalid {key} selector")
            return tuple(values)

        return SubagentRole(
            header.name,
            header.description,
            text[match.end() :].strip(),
            names("tools"),
            names("skills"),
            names("mcp"),
        )
    except (yaml.YAMLError, ValueError) as exc:
        raise ValueError(f"invalid subagent role: {path.name}") from exc


def load_roles(paths: UserAgentPaths) -> dict[str, SubagentRole]:
    roles = {}

    def add(path: Path, content: bytes) -> None:
        role = _parse(path, content)
        if role.name in roles:
            raise ValueError(f"duplicate subagent role: {role.name}")
        roles[role.name] = role

    for name in ("general", "report", "data_fetcher"):
        path = _PRESETS / f"{name}.md"
        add(path, path.read_bytes())
    # Trust the configured home, not aliases or links under an agent's workspace.
    agent_id = require_safe_name(paths.agent_id)
    if PureWindowsPath(agent_id).drive:
        raise ValueError("invalid agent directory")
    home = paths.home.resolve()
    directory = home / paths.workspace.relative_to(paths.home) / "agents" / agent_id
    if directory.resolve() != directory or not directory.is_relative_to(home):
        raise ValueError("subagent directory outside its workspace")
    if directory.exists() and not directory.is_dir():
        raise ValueError("invalid subagent directory")
    for path in sorted(directory.glob("*.md")):
        if len(roles) >= 131:
            raise ValueError("too many workspace subagent roles")
        if path.resolve() != path:
            raise ValueError("linked subagent role is not supported")
        with open_resolved_workspace_file(home, path) as stream:
            add(path, stream.read(_MAX_BYTES + 1))
    return roles
