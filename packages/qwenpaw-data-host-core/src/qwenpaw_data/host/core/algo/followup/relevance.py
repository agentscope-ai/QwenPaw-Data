# -*- coding: utf-8 -*-
"""Entity intelligence: which metric the turn was about, and what sits near it.

The collector's raw entity set is a superset: one domain-wide listing can name
dozens of dimensions that have nothing to do with the question. Ranking them
here rather than while collecting is deliberate — the user's target metric often
only becomes clear at the very end, so discarding entities mid-run would
irreversibly drop ones that turn out to matter.

Everything here is deterministic and O(n) over the touched entities, with no
extra tool calls, so it can run inside ``freeze()``. Datasets are ranked for
internal use only: SQL against a table marks the metrics and dimensions it
touches, but a physical table name never reaches the model or the user.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from qwenpaw_data.host.core.algo.followup.models import (
    EntityRecord,
    Provenance,
    SignalSnapshot,
)
from qwenpaw_data.host.core.algo.followup.settings import (
    FUZZY_COVERAGE,
    FUZZY_MIN_CHARS,
    MAX_DIMENSIONS,
    MAX_METRICS,
    MIN_RELEVANCE,
)

_RATIO_HINTS = ("人均", "日均", "月均", "均值", "平均", "率", "占比")
_COUNT_HINTS = ("个数", "人数", "次数", "用户数", "客户数")
_TIME_HINTS = ("日期", "月份", "年份", "周", "时间", "date", "month")
_STOPWORDS = ("帮我", "查询", "查看", "看看", "一下", "我需要", "我想", "的", "了", "请")
_PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)

_W_BOUND = 0.40
_W_SQL = 0.30
_W_TARGETED = 0.15
# search_context is scoped to the user's own query, so what it returns is mildly
# relevant even without an anchor binding, unlike a domain-wide listing.
_W_CTX_BOUND = 0.10
_W_RECENCY = 0.10
_W_DUMP_PENALTY = 0.30
# A dimension drillable on some other touched metric rather than on the anchor.
# Bindings are reported per metric, so a shared dimension is often attached only
# to whichever metric was searched first.
_SIBLING_BOUND = 0.6

_ANCHOR_TYPE_BONUS = 0.3
_ANCHOR_TYPE_PENALTY = 0.2
_ANCHOR_TARGETED_BONUS = 0.2

_SQL_PROVENANCE: frozenset[Provenance] = frozenset({"sql_groupby", "sql_where"})


@dataclass
class EntityEvidence:
    """What the collector saw of one entity, before any ranking.

    Attributes:
        name: Canonical entity name.
        provenance: Which kinds of tool traffic touched it.
        analyzed: Whether the run actually worked on it.
        aliases: Alternative names the semantic layer reported.
        columns: Physical column names bound to it, for matching against SQL.
        touches: How many times it was touched.
        last_pos: Event index of the most recent touch.
        parent: Coarser dimension above it in a hierarchy, when known.
        is_time: Whether it denotes time.
    """

    name: str
    provenance: set[Provenance] = field(default_factory=set)
    analyzed: bool = False
    aliases: tuple[str, ...] = ()
    columns: set[str] = field(default_factory=set)
    touches: int = 1
    last_pos: int = 0
    parent: str = ""
    is_time: bool = False


@dataclass(frozen=True)
class RankedEntities:
    """The anchor plus the pruned entity sets the snapshot is built from."""

    anchor: str
    metrics: tuple[EntityRecord, ...]
    dimensions: tuple[EntityRecord, ...]
    datasets: tuple[EntityRecord, ...]
    unused_dimensions: tuple[str, ...]
    business_entities: tuple[str, ...]
    aliases: tuple[tuple[str, str], ...]


def normalize(text: str) -> str:
    """Strip case, filler words and punctuation for lexical comparison."""
    text = text.casefold()
    for word in _STOPWORDS:
        text = text.replace(word, "")
    return _PUNCT_RE.sub("", text)


def _bigrams(text: str) -> set[str]:
    return {text[i : i + 2] for i in range(len(text) - 1)} or {text}


def dice(left: str, right: str) -> float:
    """Character-bigram Dice coefficient of two strings.

    Bigram overlap rather than whole-string ratio: a metric name occupies only
    a short span of a query, so comparing the whole strings would dilute it
    with the phrasing around it.
    """

    first, second = normalize(left), normalize(right)
    if not first or not second:
        return 0.0
    left_grams, right_grams = _bigrams(first), _bigrams(second)
    return 2 * len(left_grams & right_grams) / (len(left_grams) + len(right_grams))


def _is_ratio(name: str) -> bool:
    return any(hint in name for hint in _RATIO_HINTS)


def _is_count(name: str) -> bool:
    return any(hint in name for hint in _COUNT_HINTS)


def is_time_dimension(name: str, dimension_type: str = "") -> bool:
    """Report whether a dimension denotes time, by its name or declared type."""
    return "时间" in dimension_type or any(hint in name for hint in _TIME_HINTS)


def select_anchor(metrics: list[EntityEvidence], user_input: str) -> str:
    """Pick the metric the user is actually asking about.

    Lexical affinity with the question, plus a ratio-versus-count type prior and
    evidence of a lookup by name. Ties fall to the most recently touched metric
    rather than the first: touch order is arbitrary, and asking for a per-user
    average would otherwise be anchored on whichever count was searched first.
    """

    candidates = [metric for metric in metrics if metric.analyzed] or metrics
    if not candidates:
        return ""

    ratio_query = any(hint in user_input for hint in _RATIO_HINTS)
    count_query = any(hint in user_input for hint in _COUNT_HINTS)

    def score(metric: EntityEvidence) -> tuple[float, int]:
        lexical = max(
            dice(name, user_input) for name in (metric.name, *metric.aliases)
        )
        type_prior = 0.0
        if ratio_query:
            type_prior += _ANCHOR_TYPE_BONUS if _is_ratio(metric.name) else 0.0
            type_prior -= _ANCHOR_TYPE_PENALTY if _is_count(metric.name) else 0.0
        if count_query and _is_count(metric.name):
            type_prior += _ANCHOR_TYPE_BONUS
        call = _ANCHOR_TARGETED_BONUS if "targeted" in metric.provenance else 0.0
        return lexical + type_prior + call, metric.last_pos

    return max(candidates, key=score).name


def score_relevance(
    entity: EntityEvidence,
    anchor: EntityEvidence | None,
    anchor_dimensions: set[str],
    sibling_dimensions: set[str] | None = None,
) -> float:
    """Score how close an entity sits to the anchor, in [0, 1].

    Relatedness is measured against the anchor rather than the whole session, so
    a recommendation stays on the topic the user just raised instead of
    wandering back to something from earlier in the run.
    """

    if anchor is not None and entity.name == anchor.name:
        return 1.0

    if entity.name in anchor_dimensions:
        bound = 1.0
    elif sibling_dimensions and entity.name in sibling_dimensions:
        bound = _SIBLING_BOUND
    elif anchor is not None and "metric_bound" not in entity.provenance:
        # Metrics carry no drillable-dimension binding, so lexical kinship with
        # the anchor stands in for one (GAAP总额 next to 人均GAAP).
        bound = dice(entity.name, anchor.name)
    else:
        bound = 0.0
    sql = 1.0 if entity.provenance & _SQL_PROVENANCE else 0.0
    targeted = 1.0 if "targeted" in entity.provenance else 0.0
    ctx_bound = 1.0 if "metric_bound" in entity.provenance else 0.0
    recency = min(1.0, entity.touches / 3)
    dump_only = 1.0 if entity.provenance == {"domain_dump"} else 0.0

    score = (
        _W_BOUND * bound
        + _W_SQL * sql
        + _W_TARGETED * targeted
        + _W_CTX_BOUND * ctx_bound
        + _W_RECENCY * recency
        - _W_DUMP_PENALTY * dump_only
    )
    return min(1.0, max(0.0, score))


def rank_entities(
    metrics: dict[str, EntityEvidence],
    dimensions: dict[str, EntityEvidence],
    datasets: dict[str, EntityEvidence],
    user_input: str,
    metric_dimensions: dict[str, set[str]],
    max_metrics: int = MAX_METRICS,
    max_dimensions: int = MAX_DIMENSIONS,
    min_relevance: float = MIN_RELEVANCE,
) -> RankedEntities:
    """Choose the anchor and prune the long tail down to prompt material.

    Args:
        metrics: Metric evidence keyed by name.
        dimensions: Dimension evidence keyed by name.
        datasets: Dataset evidence keyed by name.
        user_input: The question this Chat answered.
        metric_dimensions: Metric name to the dimensions drillable on it.
        max_metrics: Cap on metrics entering the prompt.
        max_dimensions: Cap on dimensions entering the prompt.
        min_relevance: Score an entity must reach to enter the prompt.
    """

    business_entities = tuple(sorted({*metrics, *dimensions}))
    anchor_name = select_anchor(list(metrics.values()), user_input)
    anchor = metrics.get(anchor_name)
    anchor_dimensions = metric_dimensions.get(anchor_name, set())
    sibling_dimensions = {
        dimension
        for metric, bound in metric_dimensions.items()
        if metric != anchor_name
        for dimension in bound
    }

    def rank(pool: dict[str, EntityEvidence], cap: int) -> tuple[EntityRecord, ...]:
        scored = [
            EntityRecord(
                name=evidence.name,
                analyzed=evidence.analyzed,
                relevance=score_relevance(
                    evidence, anchor, anchor_dimensions, sibling_dimensions
                ),
            )
            for evidence in pool.values()
        ]
        scored.sort(key=lambda record: record.relevance, reverse=True)
        return tuple(
            record for record in scored if record.relevance >= min_relevance
        )[:cap]

    ranked_dimensions = rank(dimensions, max_dimensions)
    # Granularity gate: once a run has looked at daily numbers, offering to
    # split by month is a step backwards, so a coarser parent of an analyzed
    # dimension is not worth drilling into.
    superseded = {
        evidence.parent
        for evidence in dimensions.values()
        if evidence.analyzed and evidence.parent
    }
    unused = tuple(
        record.name
        for record in ranked_dimensions
        if not record.analyzed and record.name not in superseded
    )
    return RankedEntities(
        anchor=anchor_name,
        metrics=rank(metrics, max_metrics),
        dimensions=ranked_dimensions,
        datasets=rank(datasets, max_dimensions),
        unused_dimensions=unused,
        business_entities=business_entities,
        aliases=tuple(
            (alias, evidence.name)
            for pool in (metrics, dimensions)
            for evidence in pool.values()
            for alias in evidence.aliases
            if alias and alias != evidence.name
        ),
    )


def attribute_entities(text: str, snapshot: SignalSnapshot) -> list[str]:
    """Decide which of the Chat's real entities a question refers to.

    The model is not asked to report entities, so they are recovered from the
    question text in three passes, each cheaper to trust than the next: exact
    substring (longest name first, so 人均GAAP wins over GAAP), then aliases,
    then — only when nothing matched at all — character coverage, which
    tolerates reordering (GAAP月度 for 月度GAAP) and elision.
    """

    names = sorted(snapshot.entity_names(), key=len, reverse=True)
    folded = text.casefold()
    found: list[str] = []

    for name in names:
        lowered = name.casefold()
        if lowered in folded and not any(
            lowered in seen.casefold() for seen in found
        ):
            found.append(name)

    for alias, canonical in snapshot.alias_map().items():
        if canonical not in found and alias.casefold() in folded:
            found.append(canonical)

    if not found:
        normalized = set(normalize(text))
        for name in names:
            chars = set(normalize(name))
            if len(chars) < FUZZY_MIN_CHARS:
                continue
            if len(chars & normalized) / len(chars) >= FUZZY_COVERAGE:
                found.append(name)
                break
    return found


__all__ = [
    "EntityEvidence",
    "RankedEntities",
    "attribute_entities",
    "dice",
    "is_time_dimension",
    "normalize",
    "rank_entities",
    "score_relevance",
    "select_anchor",
]
