# -*- coding: utf-8 -*-
"""Turn chat agent-view files into artifacts the frontend can open.

The candidate set is the host ``artifact_delta`` files whose producing
tool_result seq falls inside the segment coverage. Verification stays
two-level: the candidate must be in that set, and ``list_session_files`` must
still find it under the artifact root. The listing supplies the
``relative_path`` handed to the frontend, so a name the model invented can
never become a download link. What the model adds on top is labelling — a
description, a kind and a role — over a candidate set it cannot extend.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from posixpath import basename, splitext
from typing import Any

from qwenpaw_data.host.core.algo.biztrace.models import Artifact

logger = logging.getLogger(__name__)

_NAME_TRIM = " \t\n`\"'《》「」()（）"

_QUERY_EXTS = frozenset({".sql"})
_DATASET_EXTS = frozenset({".csv", ".tsv", ".parquet", ".xlsx"})
_REPORT_EXTS = frozenset({".md", ".pdf", ".txt"})
_DASHBOARD_EXTS = frozenset({".html", ".htm"})
# Files a run writes about itself rather than for the user.
_SUPPORTING_NAMES = frozenset(
    {"problem.json", "schema.json", "metrics.json", "nl2sql_prompt.md"}
)
_INTERMEDIATE_MARKERS = ("/steps/", "/tmp/", "/raw/", "/intermediate/", "/data/raw/")
_DISPLAYABLE_KINDS = frozenset({"dataset", "report", "dashboard"})
_KIND_VALUES = frozenset({"query_script", "dataset", "report", "dashboard", "other"})
_ROLE_VALUES = frozenset({"intermediate", "final", "supporting"})

# Shaped like ``utils.workspace.list_session_files``: the host owns the listing,
# and its ``rel_path`` is what a download URL is built from.
WorkspaceLister = Callable[[], list[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class ArtifactProposal:
    """One artifact entry as the extractor wrote it, before verification.

    ``kind`` and ``role`` may be empty: a small model skips them, and the
    workspace path is a better guess than a wrong label anyway.
    """

    name: str
    description: str = ""
    kind: str = ""
    role: str = ""


def resolve_artifact(name: str, files: Mapping[str, str]) -> str | None:
    """Match a proposed artifact name against files that were actually saved.

    A model names a file loosely — with its directory, in quotes, or wrapped in
    prose. Only the filename is trusted, and only when exactly one candidate
    fits, so a vague name never resolves to an arbitrary file.
    """

    candidate = basename(name.strip(_NAME_TRIM))
    if candidate in files:
        return candidate
    matches = [filename for filename in files if filename in name]
    if len(matches) == 1:
        return matches[0]
    return None


def infer_artifact_meta(name: str, path: str) -> tuple[str, str, str]:
    """Infer kind / role / a short description from a filename and its path.

    This is what a candidate the model never labelled falls back to, so an
    unmentioned final report still reaches the card.
    """

    lower = name.lower()
    # Rooted so a marker matches a relative path too: tools write both
    # "steps/x.csv" and "/session/steps/x.csv".
    path_lower = "/" + path.lower().replace("\\", "/").lstrip("/")
    ext = splitext(lower)[1]
    intermediate = any(marker in path_lower for marker in _INTERMEDIATE_MARKERS)

    if lower in _SUPPORTING_NAMES or ext == ".py":
        return "other", "supporting", "探测或生成脚本"
    if ext in _QUERY_EXTS:
        return "query_script", "supporting", "查询脚本"
    if ext in _DASHBOARD_EXTS:
        if "dashboard" in lower or "dashboard" in path_lower:
            return "dashboard", "final", "交互式看板"
        return "report", "final", "HTML 报告"
    if ext in _REPORT_EXTS:
        if intermediate:
            return "other", "supporting", "中间说明文件"
        return "report", "final", "分析报告"
    if ext in _DATASET_EXTS:
        if intermediate:
            return "dataset", "intermediate", "中间结果数据"
        return "dataset", "final", "结果数据"
    return "other", "supporting", "工作区文件"


def is_displayable_artifact(*, kind: str, role: str, relative_path: str | None) -> bool:
    """Whether an artifact passes the frontend keep filter.

    Keep query scripts and final datasets / reports / dashboards. Everything
    else — intermediate CSVs, probing files, generator scripts, pathless rows —
    stays out of the segment card.
    """

    if not relative_path:
        return False
    if kind == "query_script":
        return True
    return role == "final" and kind in _DISPLAYABLE_KINDS


def normalize_kind(raw: str | None, fallback: str) -> str:
    """Return a known artifact kind, else ``fallback``."""
    value = (raw or "").strip()
    return value if value in _KIND_VALUES else fallback


def normalize_role(raw: str | None, fallback: str) -> str:
    """Return a known artifact role, else ``fallback``."""
    value = (raw or "").strip()
    return value if value in _ROLE_VALUES else fallback


class ArtifactVerifier:
    """Turn the files a segment produced into links the workspace can back.

    Verification is deliberately two-level: the host must have registered the
    file for this segment, and the file must still be listed under the artifact
    root. The listing wins on the path, so ``relative_path`` is always something
    the frontend can fetch.
    """

    def __init__(self, lister: WorkspaceLister | None = None) -> None:
        self.lister = lister
        self.unverified = 0

    def verify(
        self,
        proposals: Iterable[ArtifactProposal],
        *,
        files: Mapping[str, str],
    ) -> list[Artifact] | None:
        """Return the displayable artifacts among the files the segment produced.

        The candidate set is ``files`` and nothing else. A proposal that fits a
        candidate improves its description, kind and role; a candidate nobody
        named is inferred from its path, so a file the model forgot is not lost.

        Args:
            proposals: What the extractor named, in its own words.
            files: Filename-to-path map of what this segment produced.
        """

        labels = self._resolve(proposals, files)
        listed = self._listing()
        artifacts: list[Artifact] = []
        filtered: list[str] = []
        for filename, path in sorted(files.items()):
            label = labels.get(filename)
            rel_path = _match_listing(filename, path, listed)
            if rel_path is None:
                if label is not None:
                    self.unverified += 1
                    logger.info(
                        "artifact %s is not in the workspace listing", filename
                    )
                continue
            kind_guess, role_guess, description_guess = infer_artifact_meta(
                filename, path
            )
            kind = normalize_kind(label.kind if label else "", kind_guess)
            role = normalize_role(label.role if label else "", role_guess)
            if not is_displayable_artifact(
                kind=kind, role=role, relative_path=rel_path
            ):
                filtered.append(f"{filename}({kind}/{role})")
                continue
            description = (label.description if label else "") or description_guess
            artifacts.append(
                Artifact(
                    name=filename,
                    description=description,
                    relative_path=rel_path,
                    kind=kind,  # type: ignore[arg-type]
                    role=role,  # type: ignore[arg-type]
                )
            )
        # Logged as a summary so a run that keeps nothing is distinguishable
        # from one that had no candidate to begin with.
        logger.info(
            "artifact verification: %d candidates, %d kept, %d filtered%s",
            len(files),
            len(artifacts),
            len(filtered),
            f" ({', '.join(filtered)})" if filtered else "",
        )
        return artifacts or None

    def _resolve(
        self,
        proposals: Iterable[ArtifactProposal],
        files: Mapping[str, str],
    ) -> dict[str, ArtifactProposal]:
        """Key the proposals by the candidate they name, counting the misses."""
        labels: dict[str, ArtifactProposal] = {}
        for proposal in proposals:
            filename = resolve_artifact(proposal.name, files)
            if filename is None:
                self.unverified += 1
                continue
            labels.setdefault(filename, proposal)
        return labels

    def _listing(self) -> list[str]:
        if self.lister is None:
            return []
        try:
            listed = self.lister()
        except Exception:
            logger.exception("Workspace listing failed; dropping all artifacts")
            return []
        return [str(item.get("rel_path") or "") for item in listed]


def _match_listing(filename: str, recorded_path: str, listed: list[str]) -> str | None:
    """Locate the listed file, preferring the exact path the tool wrote."""
    tail = recorded_path.replace("\\", "/").lstrip("./")
    if tail in listed:
        return tail
    matches = [rel_path for rel_path in listed if basename(rel_path) == filename]
    if len(matches) == 1:
        return matches[0]
    return None


__all__ = [
    "ArtifactProposal",
    "ArtifactVerifier",
    "WorkspaceLister",
    "infer_artifact_meta",
    "is_displayable_artifact",
    "normalize_kind",
    "normalize_role",
    "resolve_artifact",
]
