"""正式/待审字典读入时先解析 synonyms，不能提前拆坏列表字符串。"""

import pytest

from profiling.algo.contexts.base import Lifecycle
from profiling.algo.contexts.dictionary import DictContext
from profiling.algo.handlers.caliber import _SemanticCatalog
from profiling.algo.inputs.probe_facts_input import StaticProbeFactsInput
from profiling.algo.tasks import ProfileTask


@pytest.mark.parametrize("lifecycle", ["confirmed", "staged"])
@pytest.mark.parametrize(
    "kind,name_key,frame_key",
    [
        ("dimension", "dimension_name", "dimensions"),
        ("metric", "metric_name", "metrics"),
    ],
)
@pytest.mark.parametrize(
    "value,expected",
    [
        ("[foo$$$bar]", []),
        ("['foo', bar]", []),
        ("foo$$$bar", ["foo", "bar"]),
        ("foo, bar", ["foo", "bar"]),
        ('["foo", "bar"]', ["foo", "bar"]),
        ("['foo', 'bar']", ["foo", "bar"]),
        (["[foo]", "bar [USD]"], ["[foo]", "bar [USD]"]),
        ('["[foo]", "bar [USD]", "foo$$$bar"]', ["[foo]", "bar [USD]", "foo$$$bar"]),
    ],
)
async def test_fact_synonyms_are_parsed_before_snapshot_normalization(
    lifecycle,
    kind,
    name_key,
    frame_key,
    value,
    expected,
    caplog,
):
    fact_key = kind if lifecycle == "confirmed" else f"staged_{kind}"
    inputs = StaticProbeFactsInput(
        {
            fact_key: [
                {
                    "id": 7,
                    "domain_name": "sales",
                    name_key: "Canonical [USD]",
                    "description": "正式描述",
                    "synonyms": value,
                    "dimension_type": "categorical",
                    "probe_status": "CONFIRMED",
                }
            ]
        }
    )
    snapshot = await inputs.facts_of("columns_dict", lifecycle)()
    frame = getattr(snapshot, frame_key)

    assert frame[name_key].tolist() == ["Canonical [USD]"]
    assert frame["description"].tolist() == ["正式描述"]
    assert frame["synonyms"].tolist() == [expected]
    if not expected:
        assert "Invalid synonyms list; aliases ignored" in caplog.text
    else:
        assert "Invalid synonyms list; aliases ignored" not in caplog.text
    if lifecycle == "staged":
        assert frame["probe_status"].tolist() == ["CONFIRMED"]

    if lifecycle == "confirmed":
        context = DictContext(
            name="columns_dict_confirmed",
            lifecycle=Lifecycle.CONFIRMED,
            fact=inputs.facts_of("columns_dict", lifecycle),
        )
        await context.load()
        catalog = _SemanticCatalog.from_evidence(
            context.evidence_for_task(ProfileTask.CALIBER)
        )
        assert catalog.match(kind, "sales", "Canonical [USD]") == "Canonical [USD]"
        assert catalog.prompt_records(kind, "sales")[0]["synonyms"] == expected
        for alias in expected:
            assert catalog.match(kind, "sales", alias) == "Canonical [USD]"
        for fragment in ("[foo", "bar]", "['foo'", "'bar']"):
            assert catalog.match(kind, "sales", fragment) is None
