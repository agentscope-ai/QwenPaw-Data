# -*- coding: utf-8 -*-
"""Entity linking: turn business terms in a card body into markdown links.

Runs entirely in memory after a card body is generated. Captions are left
alone, and the substitution happens once per card, so a written card's links
never change afterwards.
"""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlencode

from qwenpaw_data.host.core.algo.biztrace.semantic_vocab import (
    SemanticVocabulary,
    VocabEntry,
)
from qwenpaw_data.host.core.algo.biztrace.settings import BizTraceSettings, resolve_link_base_url
from qwenpaw_data.host.core.cm_client import resolve_cm_base_url

logger = logging.getLogger(__name__)

ENTITY_ROUTES: dict[str, tuple[str, str | None]] = {
    "biz_domain": ("business-domain", None),
    "dimension": ("dimension", "dimension_id"),
    "metric": ("metric-lib", "metric_id"),
    "dataset": ("data-set", "dataset_id"),
}

# Regions where a substitution would break rendering or nest a link.
# Inline code stays protected in the main scan: a link nested inside
# backticks is not clickable. Exact `` `term` `` / `` **term** `` spans are
# handled separately by ``_link_marked_terms`` before this pass.
_PROTECTED_RE = re.compile(
    r"```.*?```"  # fenced code
    r"|`[^`\n]*`"  # inline code
    r"|!?\[[^\]\n]*\]\([^)\n]*\)"  # markdown link or image
    # Autolink or raw tag, attributes included: segment fields carry
    # <span class="..."> around key numbers, and a link inside a tag's
    # attributes would render as literal markdown.
    r"|<[^>\n]*>"
    r"|\bhttps?://\S+",  # bare URL
    re.DOTALL,
)
# Emphasis / code wrappers the model often puts around entity names.
_MARKED_TERM_RE = re.compile(
    r"`([^`\n]+)`"
    r"|\*\*([^*]+)\*\*"
    r"|__([^_]+)__",
)
_WORD_CHAR_RE = re.compile(r"[0-9A-Za-z_]")


def build_entity_url(
    entry: VocabEntry, *, base_url: str, datasource_id: str | None
) -> str | None:
    """Render the Context frontend URL an entry points at."""
    route = ENTITY_ROUTES.get(entry.entity_type)
    if route is None:
        return None
    page, id_param = route
    params: list[tuple[str, str]] = []
    if datasource_id:
        params.append(("datasource_id", datasource_id))
    if entry.domain_id:
        params.append(("domain_id", entry.domain_id))
    if id_param and entry.entity_id:
        params.append((id_param, entry.entity_id))
    query = urlencode(params)
    return f"{base_url}/{page}?{query}" if query else f"{base_url}/{page}"


class EntityLinker:
    """Longest-first exact matcher that injects inline markdown links."""

    def __init__(
        self,
        *,
        terms: dict[str, VocabEntry],
        base_url: str,
        datasource_id: str | None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.datasource_id = datasource_id
        self._urls: dict[str, str] = {}
        for term, entry in terms.items():
            url = build_entity_url(
                entry, base_url=self.base_url, datasource_id=datasource_id
            )
            if url is not None:
                self._urls[term] = url
        self._lengths = sorted({len(term) for term in self._urls}, reverse=True)

    def __bool__(self) -> bool:
        return bool(self._urls)

    def link(self, body: str) -> str:
        """Link every occurrence of every known term outside protected spans."""
        if not body or not self._urls:
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
            out.append(f"[{term}]({self._urls[term]})")
            index += len(term)
        return "".join(out)

    def _link_marked_terms(self, body: str) -> str:
        """Turn exact `` `term` `` / `` **term** `` / `` __term__ `` into links.

        Wrappers are dropped so the markdown link is clickable. Keeping
        backticks in the label (`` [`term`](url) ``) is not usable here: the
        chat Markdown ``code`` component is a fenced CodeHighlighter and does
        not render nested inline code inside links.
        """

        def replace(match: re.Match[str]) -> str:
            term = match.group(1) or match.group(2) or match.group(3) or ""
            if not term or term not in self._urls:
                return match.group(0)
            return f"[{term}]({self._urls[term]})"

        return _MARKED_TERM_RE.sub(replace, body)

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


def _protected_end(protected: list[tuple[int, int]], index: int) -> int | None:
    for start, end in protected:
        if start <= index < end:
            return end
        if start > index:
            break
    return None


def _breaks_word(body: str, start: int, end: int) -> bool:
    """Reject a match that is only part of a longer ASCII identifier."""
    if _WORD_CHAR_RE.match(body[start]) and start > 0:
        if _WORD_CHAR_RE.match(body[start - 1]):
            return True
    if _WORD_CHAR_RE.match(body[end - 1]) and end < len(body):
        if _WORD_CHAR_RE.match(body[end]):
            return True
    return False


class VocabularyLinker:
    """Link against the shared vocabulary, picking up its refreshes.

    The matcher is rebuilt whenever the vocabulary swaps its term index, so a
    chat that started before the first fetch landed still links its later
    cards. Until then ``link`` is the identity function.
    """

    def __init__(
        self,
        vocabulary: SemanticVocabulary,
        *,
        base_url: str,
        datasource_id: str | None,
    ) -> None:
        self.vocabulary = vocabulary
        self.base_url = base_url
        self.datasource_id = datasource_id
        self._terms: dict[str, VocabEntry] | None = None
        self._linker: EntityLinker | None = None

    def link(self, body: str) -> str:
        terms = self.vocabulary.terms
        if terms is not self._terms:
            self._terms = terms
            self._linker = (
                EntityLinker(
                    terms=terms,
                    base_url=self.base_url,
                    datasource_id=self.datasource_id,
                )
                if terms
                else None
            )
        return self._linker.link(body) if self._linker is not None else body


_VOCABULARIES: dict[tuple[str, str | None], SemanticVocabulary] = {}
_REFRESHES: set[asyncio.Task[None]] = set()


def build_linker(
    settings: BizTraceSettings,
    *,
    datasource_id: str | None,
    access_token: str | None = None,
) -> VocabularyLinker | None:
    """Return a linker over the shared vocabulary, or None if disabled.

    Synchronous on purpose: this runs while the agent is starting, so it only
    kicks off the fetch. Vocabularies are shared per ``(cm_base_url,
    datasource_id)``; markdown hrefs use the Context frontend origin. The
    latest chat's access token is used for subsequent CM refreshes.
    """

    if not settings.biz_link_enabled:
        return None
    resolved = (settings.biz_link_datasource_id or datasource_id or "").strip()
    token = (access_token or "").strip() or None
    # 空串表示同源相对路径（/metrics?...），不能当 falsy 跳过。
    link_base_url = resolve_link_base_url(settings)
    api_base_url = resolve_cm_base_url()
    if not api_base_url:
        return None
    key = (api_base_url, resolved or None)
    vocabulary = _VOCABULARIES.get(key)
    if vocabulary is None:
        vocabulary = SemanticVocabulary(
            base_url=api_base_url,
            datasource_id=resolved or None,
            access_token=token,
            ttl=settings.biz_link_ttl,
        )
        _VOCABULARIES[key] = vocabulary
    else:
        # Keep the shared cache, but refresh with the newest chat's credentials.
        vocabulary.access_token = token
    _refresh(vocabulary)
    return VocabularyLinker(
        vocabulary, base_url=link_base_url, datasource_id=resolved or None
    )


def _refresh(vocabulary: SemanticVocabulary) -> None:
    """Kick off a TTL refresh in the background; ``ensure_fresh`` never raises."""
    task = asyncio.create_task(vocabulary.ensure_fresh())
    _REFRESHES.add(task)
    task.add_done_callback(_REFRESHES.discard)


async def shutdown_vocabularies() -> None:
    """Close the shared vocabulary clients at application shutdown."""
    for task in list(_REFRESHES):
        task.cancel()
    _REFRESHES.clear()
    for vocabulary in list(_VOCABULARIES.values()):
        await vocabulary.aclose()
    _VOCABULARIES.clear()


__all__ = [
    "ENTITY_ROUTES",
    "EntityLinker",
    "VocabularyLinker",
    "build_entity_url",
    "build_linker",
    "shutdown_vocabularies",
]
