# -*- coding: utf-8 -*-
"""Turn verified artifact path mentions in a reply into panel links.

The listing decides which paths exist. A name the model invented never
becomes a link. Substitution happens once per card, the same way entity
linking does: captions stay untouched, and a written card's links never
change afterwards.

The href is a fragment (``#artifact:rel_path``) rather than a custom
scheme so DOMPurify's default URI allowlist keeps the attribute. The
frontend resolves it at click time and opens the in-app preview panel.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from posixpath import basename
from urllib.parse import quote

from qwenpaw_data.host.core.algo.biztrace.linking import (
    _PROTECTED_RE,
    _breaks_word,
    _protected_end,
)
from qwenpaw_data.host.core.algo.biztrace.workspace_index import WorkspaceLister

logger = logging.getLogger(__name__)

ARTIFACT_HREF_PREFIX = "#artifact:"
# Allow newlines inside wrappers: the model often line-wraps a host path
# inside backticks, and ``linking._MARKED_TERM_RE`` stops at ``\n``.
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_PATH_MARKED_RE = re.compile(
    r"`([^`]+)`"
    r"|\*\*([^*]+)\*\*"
    r"|__([^_]+)__",
    re.DOTALL,
)
# ``artifacts/<session>/`` anywhere in a host or workdir path.
_ARTIFACTS_DIR_RE = re.compile(r"(?:^|/)artifacts/[^/]+/")


def artifact_href(rel_path: str) -> str:
    """Build the fragment sentinel the frontend ``parseArtifactHref`` reads."""
    return f"{ARTIFACT_HREF_PREFIX}{quote(rel_path, safe='/')}"


def _normalize_mention(mention: str) -> str:
    """Posix-ify a mention and drop wrap whitespace inside a path."""
    text = mention.replace("\\", "/").strip()
    if "/" in text:
        text = re.sub(r"\s+", "", text)
    if text.startswith("./"):
        text = text[2:]
    return text


class ArtifactLinker:
    """Longest-first matcher that injects ``#artifact:`` markdown links."""

    def __init__(
        self, *, session_id: str, lister: WorkspaceLister | None = None
    ) -> None:
        self.session_id = session_id
        self.lister = lister
        self._ready = False
        self._by_rel: dict[str, str] = {}
        self._by_basename: dict[str, str] = {}
        self._urls: dict[str, str] = {}
        self._lengths: list[int] = []

    def invalidate(self) -> None:
        """Drop the cached listing so the next ``link`` re-reads the workspace."""
        self._ready = False
        self._by_rel = {}
        self._by_basename = {}
        self._urls = {}
        self._lengths = []

    def link(self, body: str) -> str:
        """Link every verified artifact mention outside protected spans."""
        if not body:
            return body
        self._ensure_index()
        if not self._urls:
            return body
        body = self._link_marked_terms(body)
        protected = [match.span() for match in _PROTECTED_RE.finditer(body)]
        out: list[str] = []
        index = 0
        length = len(body)
        while index < length:
            skip_to = _protected_end(protected, index)
            if skip_to is not None:
                out.append(body[index:skip_to])
                index = skip_to
                continue
            term = self._match_at(body, index)
            if term is None:
                out.append(body[index])
                index += 1
                continue
            out.append(f"[{term}]({artifact_href(self._urls[term])})")
            index += len(term)
        return "".join(out)

    def _link_marked_terms(self, body: str) -> str:
        """Turn exact `` `path` `` / `` **path** `` / `` __path__ `` into links.

        Wrappers are dropped so the markdown link is clickable. A link
        nested inside backticks is not.
        """

        def replace(match: re.Match[str]) -> str:
            term = match.group(1) or match.group(2) or match.group(3) or ""
            rel_path = self._resolve(term)
            if rel_path is None:
                return match.group(0)
            return f"[{term}]({artifact_href(rel_path)})"

        parts: list[str] = []
        index = 0
        for fence in _FENCE_RE.finditer(body):
            parts.append(_PATH_MARKED_RE.sub(replace, body[index : fence.start()]))
            parts.append(fence.group(0))
            index = fence.end()
        parts.append(_PATH_MARKED_RE.sub(replace, body[index:]))
        return "".join(parts)

    def _match_at(self, body: str, index: int) -> str | None:
        for size in self._lengths:
            end = index + size
            if end > len(body):
                continue
            candidate = body[index:end]
            if candidate not in self._urls:
                continue
            if _breaks_word(body, index, end):
                continue
            return candidate
        return None

    def _resolve(self, mention: str) -> str | None:
        """Map a written mention to a listed ``rel_path``, or None.

        Accepts a bare filename, a workspace-relative
        ``artifacts/<session>/...`` path, and a host/workdir absolute
        path that contains that segment. A unique basename is the last
        resort so a long absolute path still links when the session
        folder in the mention does not match ``self.session_id``.
        """
        normalized = _normalize_mention(mention)
        if not normalized:
            return None
        if normalized in self._by_rel:
            return normalized
        match = _ARTIFACTS_DIR_RE.search(normalized)
        if match is not None:
            tail = normalized[match.end() :]
            if tail in self._by_rel:
                return tail
            name = basename(tail.rstrip("/"))
            if name in self._by_basename:
                return self._by_basename[name]
        for rel in sorted(self._by_rel, key=len, reverse=True):
            if normalized == rel or normalized.endswith(f"/{rel}"):
                return rel
        return self._by_basename.get(basename(normalized.rstrip("/")))

    def _ensure_index(self) -> None:
        if self._ready:
            return
        self._ready = True
        paths: list[str] = []
        if self.lister is not None:
            try:
                listed = self.lister()
            except Exception:
                logger.exception("Workspace listing failed; skipping artifact links")
                listed = []
            for item in listed:
                rel = (
                    str(item.get("rel_path") or "")
                    .replace("\\", "/")
                    .strip()
                    .lstrip("./")
                )
                if rel:
                    paths.append(rel)
        self._by_rel = {path: path for path in paths}
        counts = Counter(basename(path) for path in paths)
        self._by_basename = {
            basename(path): path for path in paths if counts[basename(path)] == 1
        }
        urls: dict[str, str] = {}
        prefix = f"artifacts/{self.session_id}/"
        for rel in paths:
            urls[rel] = rel
            urls[f"{prefix}{rel}"] = rel
            name = basename(rel)
            if name in self._by_basename:
                urls[name] = rel
        self._urls = urls
        self._lengths = sorted({len(term) for term in urls}, reverse=True)


__all__ = [
    "ARTIFACT_HREF_PREFIX",
    "ArtifactLinker",
    "artifact_href",
]
