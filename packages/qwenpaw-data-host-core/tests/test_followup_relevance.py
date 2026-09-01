# -*- coding: utf-8 -*-
"""Which metric the turn was about, what sits near it, and what a question names."""

from __future__ import annotations

from typing import Any

from qwenpaw_data.host.core.algo.followup.models import Provenance, SignalSnapshot
from qwenpaw_data.host.core.algo.followup.relevance import (
    EntityEvidence,
    attribute_entities,
    dice,
    is_time_dimension,
    rank_entities,
    score_relevance,
    select_anchor,
)
from qwenpaw_data.host.core.algo.followup.settings import MAX_DIMENSIONS, MIN_RELEVANCE


def _evidence(
    name: str, *provenance: Provenance, **overrides: Any
) -> EntityEvidence:
    return EntityEvidence(name=name, provenance=set(provenance), **overrides)


def _pool(*entities: EntityEvidence) -> dict[str, EntityEvidence]:
    return {entity.name: entity for entity in entities}


# -- lexical affinity ------------------------------------------------------- #


def test_filler_words_do_not_dilute_the_similarity() -> None:
    """A metric name is a short span of a long question, so the phrasing
    around it must not decide the match."""

    assert dice("人均GAAP", "帮我查询一下人均GAAP") == 1.0


def test_a_name_absent_from_the_question_scores_nothing() -> None:
    assert dice("留存率", "看下 GAAP 消费规模") == 0.0


def test_a_time_dimension_is_recognised_by_name_or_by_type() -> None:
    assert is_time_dimension("统计日期") is True
    assert is_time_dimension("渠道类型", "时间维度") is True
    assert is_time_dimension("渠道类型") is False


# -- anchor selection ------------------------------------------------------- #


def test_a_per_user_question_anchors_on_the_ratio_not_the_count() -> None:
    """Both metrics were analyzed and the count was searched first; only the
    type prior keeps the anchor on what the user actually asked for."""

    metrics = [
        _evidence("漏斗有效付费用户数", "targeted", analyzed=True, last_pos=1),
        _evidence("人均GAAP", "metric_bound", analyzed=True, last_pos=2),
    ]

    assert select_anchor(metrics, "查询有效付费用户的人均GAAP消费规模") == "人均GAAP"


def test_a_head_count_question_anchors_on_the_count() -> None:
    metrics = [
        _evidence("人均GAAP", "targeted", analyzed=True),
        _evidence("漏斗有效付费用户数", "metric_bound", analyzed=True),
    ]

    assert select_anchor(metrics, "6月有效付费用户数是多少") == "漏斗有效付费用户数"


def test_a_lookup_by_name_breaks_a_lexical_tie() -> None:
    metrics = [
        _evidence("月度GAAP", "metric_bound", analyzed=True),
        _evidence("月度GAAP总额", "targeted", analyzed=True),
    ]

    assert select_anchor(metrics, "看下这个月的情况") == "月度GAAP总额"


def test_an_unanalyzed_metric_can_still_be_the_anchor() -> None:
    """A run that only searched the semantic layer analyzed nothing, and the
    turn is still about the metric it found."""

    metrics = [_evidence("人均GAAP", "metric_bound")]

    assert select_anchor(metrics, "人均GAAP如何") == "人均GAAP"


def test_no_metric_means_no_anchor() -> None:
    assert select_anchor([], "看下情况") == ""


# -- relevance scoring ------------------------------------------------------ #


def test_the_anchor_is_maximally_relevant_to_itself() -> None:
    anchor = _evidence("人均GAAP", "targeted", analyzed=True)

    assert score_relevance(anchor, anchor, set()) == 1.0


def test_a_dimension_the_anchor_can_be_split_by_outranks_a_stranger() -> None:
    anchor = _evidence("人均GAAP", "targeted", analyzed=True)
    bound = _evidence("渠道类型", "metric_bound")
    unbound = _evidence("MCPServerID", "metric_bound")

    assert score_relevance(bound, anchor, {"渠道类型"}) > score_relevance(
        unbound, anchor, {"渠道类型"}
    )


def test_a_dimension_bound_to_a_sibling_metric_lands_in_between() -> None:
    """Bindings are reported per metric, so a shared dimension is often only
    attached to whichever metric was searched first."""

    anchor = _evidence("人均GAAP", "targeted", analyzed=True)
    sibling = _evidence("地域", "metric_bound")
    stranger = _evidence("MCPServerID", "metric_bound")

    anchor_bound = score_relevance(
        _evidence("渠道类型", "metric_bound"), anchor, {"渠道类型"}, {"地域"}
    )
    sibling_bound = score_relevance(sibling, anchor, {"渠道类型"}, {"地域"})
    unbound = score_relevance(stranger, anchor, {"渠道类型"}, {"地域"})

    assert anchor_bound > sibling_bound > unbound


def test_an_entity_known_only_from_a_domain_listing_scores_zero() -> None:
    """This is what stops "按 MCPServerID 拆解人均GAAP": nobody asked for the
    dimension, a domain-wide dump merely named it."""

    anchor = _evidence("人均GAAP", "targeted", analyzed=True)
    dumped = _evidence("MCPServerID", "domain_dump")

    assert score_relevance(dumped, anchor, set()) == 0.0


def test_a_column_grouped_by_in_sql_earns_its_place() -> None:
    anchor = _evidence("人均GAAP", "targeted", analyzed=True)
    grouped = _evidence("渠道类型", "domain_dump", "sql_groupby")

    assert score_relevance(grouped, anchor, set()) >= MIN_RELEVANCE


# -- ranking ---------------------------------------------------------------- #


def test_the_long_tail_is_pruned_and_the_rest_is_capped() -> None:
    dimensions = _pool(
        _evidence("渠道类型", "metric_bound"),
        *(_evidence(f"维度{index}", "domain_dump") for index in range(20)),
    )
    metrics = _pool(_evidence("人均GAAP", "targeted", analyzed=True))

    ranked = rank_entities(
        metrics, dimensions, {}, "人均GAAP如何", {"人均GAAP": {"渠道类型"}}
    )

    assert ranked.anchor == "人均GAAP"
    assert [record.name for record in ranked.dimensions] == ["渠道类型"]
    assert len(ranked.dimensions) <= MAX_DIMENSIONS


def test_a_coarser_parent_is_not_offered_once_its_child_was_analyzed() -> None:
    """Having looked at days, splitting by month is a step backwards."""

    dimensions = _pool(
        _evidence("统计日期", "targeted", analyzed=True, parent="统计月份"),
        _evidence("统计月份", "metric_bound"),
        _evidence("渠道类型", "metric_bound"),
    )
    metrics = _pool(_evidence("人均GAAP", "targeted", analyzed=True))

    ranked = rank_entities(
        metrics,
        dimensions,
        {},
        "人均GAAP的日趋势",
        {"人均GAAP": {"统计日期", "统计月份", "渠道类型"}},
    )

    assert "统计月份" not in ranked.unused_dimensions
    assert "渠道类型" in ranked.unused_dimensions


def test_a_pruned_entity_can_still_be_attributed_to() -> None:
    """Pruning decides what enters the prompt, not what counts as real: a
    question naming a dumped dimension is grounded, just not promoted."""

    dimensions = _pool(_evidence("MCPServerID", "domain_dump"))
    metrics = _pool(_evidence("人均GAAP", "targeted", analyzed=True))

    ranked = rank_entities(metrics, dimensions, {}, "人均GAAP如何", {})

    assert ranked.dimensions == ()
    assert "MCPServerID" in ranked.business_entities


def test_dataset_names_are_kept_out_of_the_business_entities() -> None:
    datasets = _pool(_evidence("dwd_gaap_daily", "sql_groupby", analyzed=True))
    metrics = _pool(_evidence("人均GAAP", "targeted", analyzed=True))

    ranked = rank_entities(metrics, {}, datasets, "人均GAAP如何", {})

    assert ranked.business_entities == ("人均GAAP",)


def test_aliases_are_collected_for_attribution() -> None:
    metrics = _pool(
        _evidence("人均GAAP", "targeted", analyzed=True, aliases=("人均消费", "人均GAAP"))
    )

    ranked = rank_entities(metrics, {}, {}, "人均GAAP如何", {})

    assert ranked.aliases == (("人均消费", "人均GAAP"),)


# -- attribution ------------------------------------------------------------ #


def _snapshot(**overrides: Any) -> SignalSnapshot:
    return SignalSnapshot(**overrides)


def test_the_longest_matching_name_wins() -> None:
    snapshot = _snapshot(business_entities=("GAAP", "人均GAAP"))

    assert attribute_entities("人均GAAP还能拆吗", snapshot) == ["人均GAAP"]


def test_an_alias_is_recorded_as_its_canonical_name() -> None:
    snapshot = _snapshot(
        business_entities=("人均GAAP",),
        entity_aliases=(("人均消费", "人均GAAP"),),
    )

    assert attribute_entities("人均消费的趋势如何", snapshot) == ["人均GAAP"]


def test_a_reordered_name_is_recovered_only_as_a_last_resort() -> None:
    snapshot = _snapshot(business_entities=("月度GAAP",))

    assert attribute_entities("分析2026年GAAP月度波动的原因", snapshot) == ["月度GAAP"]


def test_a_question_naming_nothing_real_is_attributed_nothing() -> None:
    snapshot = _snapshot(business_entities=("人均GAAP",))

    assert attribute_entities("看看留存率如何", snapshot) == []
