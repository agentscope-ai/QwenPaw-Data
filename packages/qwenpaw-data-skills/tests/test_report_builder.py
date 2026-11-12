"""Contract of bi-report-generation's ``report_builder.py``.

The builder only assembles; for report reproduction the guarantee that matters
is that the generated DOM is reconciled against ``dom_contract``. A hollow shell
-- right class names in the passed-through CSS, none of the data in the body --
must NOT pass as a faithful reproduction. These tests pin that gate plus the
provenance marker that proves the HTML came out of the builder.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

SKILL_ROOT = (
    Path(__file__).resolve().parents[1] / "skills" / "atomic" / "bi-report-generation"
)
SCRIPT = SKILL_ROOT / "scripts" / "report_builder.py"


def _load_builder_module() -> Any:
    """Import the skill script by path: it ships as a file, not as a package."""
    spec = importlib.util.spec_from_file_location("_report_builder_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


builder = _load_builder_module()


# --- selector parsing --------------------------------------------------------


def test_parse_simple_selector_variants() -> None:
    assert builder._parse_simple_selector("tr.ent-row") == ("tr", ["ent-row"])
    assert builder._parse_simple_selector(".grid2") == (None, ["grid2"])
    assert builder._parse_simple_selector(".tabpanel.hide") == (
        None,
        ["tabpanel", "hide"],
    )
    assert builder._parse_simple_selector("h2.sec") == ("h2", ["sec"])
    # a combinator keeps the right-most compound: that is the element itself
    assert builder._parse_simple_selector("details.panel > summary") == ("summary", [])
    # pseudo-classes / attribute selectors are dropped, they do not change counts
    assert builder._parse_simple_selector("tr.open:hover") == ("tr", ["open"])
    assert builder._parse_simple_selector("") == (None, [])


# --- element counting --------------------------------------------------------


def test_count_selector_counts_elements_not_css_definitions() -> None:
    html = (
        "<head><style>"
        "tr.ent-row{cursor:pointer} .grid2{display:grid} td.pos{color:green}"
        "</style></head>"
        "<body><table>"
        '<tr class="ent-row"><td class="pos">1</td><td class="neg">2</td></tr>'
        '<tr class="ent-row"><td class="pos">3</td></tr>'
        "</table>"
        '<div class="grid2"><div class="grid2"></div></div>'
        "</body>"
    )
    # the <style> block names the classes but defines zero elements
    assert builder._count_selector(html, "tr.ent-row") == 2
    assert builder._count_selector(html, "td.pos") == 2
    assert builder._count_selector(html, "td.neg") == 1
    assert builder._count_selector(html, ".grid2") == 2
    assert builder._count_selector(html, "h2.sec") == 0
    assert builder._count_selector(html, "") is None


# --- reconciliation gate -----------------------------------------------------


def test_reconcile_flags_a_hollow_shell() -> None:
    spec = {
        "dom_contract": [
            {"selector": "h2.sec", "count": 5, "semantics": "section title"},
            {"selector": "tr.ent-row", "count": 1018, "semantics": "entity row"},
            {"selector": "td.pos", "count": 500, "semantics": "positive cell"},
        ]
    }
    html = (
        "<body>"
        + '<h2 class="sec">t</h2>' * 5
        + '<tr class="ent-row"></tr>' * 3
        + "</body>"
    )
    violations, checked = builder.reconcile_dom_contract(html, spec)
    assert checked == 3
    joined = "\n".join(violations)
    assert "tr.ent-row" in joined
    assert "td.pos" in joined
    # five real titles clear the floor, so the skeleton is not what fails
    assert "h2.sec" not in joined
    assert len(violations) == 2


def test_reconcile_passes_when_data_is_materialized() -> None:
    spec = {
        "dom_contract": [
            {"selector": "tr.ent-row", "count": 1018},
            {"selector": "td.pos", "count": 500},
            {"selector": ".grid2", "count": 10},
        ]
    }
    html = (
        "<body>"
        + '<tr class="ent-row"></tr>' * 700
        + '<td class="pos"></td>' * 400
        + '<div class="grid2"></div>' * 8
        + "</body>"
    )
    violations, checked = builder.reconcile_dom_contract(html, spec)
    assert checked == 3
    assert violations == []


def test_reconcile_skips_state_classes_and_zero_counts() -> None:
    spec = {
        "dom_contract": [
            {"selector": "tr.open", "count": 0, "state": True},
            {"selector": ".tabpanel.hide", "count": 0, "state": True},
            {"selector": "h2.sec", "count": 5},
        ]
    }
    html = "<body>" + '<h2 class="sec">t</h2>' * 5 + "</body>"
    violations, checked = builder.reconcile_dom_contract(html, spec)
    # interaction states are 0 in static HTML by design, so only h2.sec counts
    assert checked == 1
    assert violations == []


def test_reconcile_is_inert_without_dom_contract() -> None:
    assert builder.reconcile_dom_contract("<body></body>", {}) == ([], 0)
    assert builder.reconcile_dom_contract("<body></body>", {"dom_contract": []}) == (
        [],
        0,
    )


# --- provenance marker -------------------------------------------------------


def test_builder_marker_reports_reconcile_outcome() -> None:
    head = "<html><head><style>x</style></head><body></body></html>"

    passed = builder.inject_builder_marker(head, 3, [])
    assert "qwenpaw-data-report-builder" in passed
    assert "dom_contract: PASS (3/3" in passed
    # the stamp lands inside <head>, before the closing tag
    assert passed.index("qwenpaw-data-report-builder") < passed.index("</head>")

    failed = builder.inject_builder_marker(head, 3, ["a", "b"])
    assert "dom_contract: FAIL (2/3" in failed

    inert = builder.inject_builder_marker(head, 0, [])
    assert "dom_contract: n/a" in inert


# --- end-to-end through main() ----------------------------------------------


def _write_reproduction_inputs(tmp_path: Path, entity_rows: int) -> tuple[Path, Path]:
    """Lay out a minimal reproduction task: GT source, format_spec, one section.

    ``entity_rows`` is how many real ``tr.ent-row`` the section fragment carries,
    i.e. what ends up in the generated report.html body.
    """
    source = tmp_path / "source" / "original.html"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "<html><head><style>tr.ent-row{cursor:pointer}</style></head>"
        "<body>" + '<tr class="ent-row"></tr>' * 100 + "</body></html>",
        encoding="utf-8",
    )
    spec_path = tmp_path / "format_spec.json"
    spec_path.write_text(
        json.dumps(
            {
                "source": {"path": "source/original.html", "type": "html"},
                "source_css": "tr.ent-row{cursor:pointer}",
                "dom_contract": [
                    {"selector": "tr.ent-row", "count": 100, "semantics": "entity row"}
                ],
            }
        ),
        encoding="utf-8",
    )
    sections = tmp_path / "sections_001.json"
    fragment = (
        "<table>" + '<tr class="ent-row"><td>1</td></tr>' * entity_rows + "</table>"
    )
    sections.write_text(
        json.dumps([{"title": "S1", "html_fragment": fragment}]), encoding="utf-8"
    )
    return spec_path, sections


def _argv(
    tmp_path: Path, spec_path: Path, sections: Path, out: Path, *extra: str
) -> list[str]:
    return [
        "report_builder.py",
        "--sections",
        str(sections),
        "--format-spec",
        str(spec_path),
        "--output",
        str(out),
        *extra,
    ]


def test_main_fails_a_hollow_reproduction(tmp_path, monkeypatch, capsys) -> None:
    spec_path, sections = _write_reproduction_inputs(tmp_path, entity_rows=5)
    out = tmp_path / "report.html"
    monkeypatch.setattr(sys, "argv", _argv(tmp_path, spec_path, sections, out))

    with pytest.raises(SystemExit) as exc:
        builder.main()
    assert exc.value.code == 2

    captured = capsys.readouterr()
    assert "tr.ent-row" in captured.out
    # the shell is still written for inspection, but stamped FAIL
    assert out.exists()
    assert "dom_contract: FAIL" in out.read_text(encoding="utf-8")


def test_main_passes_a_faithful_reproduction(tmp_path, monkeypatch) -> None:
    spec_path, sections = _write_reproduction_inputs(tmp_path, entity_rows=60)
    out = tmp_path / "report.html"
    monkeypatch.setattr(sys, "argv", _argv(tmp_path, spec_path, sections, out))

    builder.main()  # must not raise

    text = out.read_text(encoding="utf-8")
    assert "qwenpaw-data-report-builder" in text
    assert "dom_contract: PASS" in text


def test_main_allow_dom_mismatch_downgrades_to_warning(tmp_path, monkeypatch) -> None:
    spec_path, sections = _write_reproduction_inputs(tmp_path, entity_rows=5)
    out = tmp_path / "report.html"
    monkeypatch.setattr(
        sys, "argv", _argv(tmp_path, spec_path, sections, out, "--allow-dom-mismatch")
    )

    builder.main()  # exit suppressed by the escape hatch

    assert "dom_contract: FAIL" in out.read_text(encoding="utf-8")


# --- declarative blocks ------------------------------------------------------

blocks = builder.report_blocks


def _write_block_data(root: Path) -> None:
    (root / "trend.csv").write_text(
        "date,gaap\n2026-10-01,10.5\n2026-10-02,12.25\n2026-10-03,9.75\n",
        encoding="utf-8",
    )
    (root / "seg.csv").write_text(
        "seg,uid,delta\n"
        + "".join(f"s{i},{100 - i},{(-1) ** i * i / 10}\n" for i in range(9)),
        encoding="utf-8",
    )
    (root / "boards.json").write_text(
        json.dumps(
            {
                "rows": [
                    {"cid": "c1", "name": "A", "w43": 5},
                    {"cid": "c2", "name": "B", "w43": 3},
                ]
            }
        ),
        encoding="utf-8",
    )
    (root / "uid.csv").write_text(
        "cid,uid,w43\nc1,u1,2\nc1,u2,3\nc2,u3,3\n", encoding="utf-8"
    )
    (root / "ret.csv").write_text(
        "cohort,w1,w2\nW40,50,30\nW41,45,25\n", encoding="utf-8"
    )


def _blocks_sections() -> list[dict[str, Any]]:
    return [
        {
            "title": "总览",
            "blocks": [
                {
                    "type": "conclusion",
                    "text": "净 GAAP **30.5** 万，环比 {-1.66%}，新客 {+12.5%}",
                },
                {
                    "type": "kpi",
                    "items": [
                        {"label": "GAAP", "value": 30.5, "fmt": "num2", "delta": -1.66}
                    ],
                },
                {
                    "type": "grid",
                    "cols": 2,
                    "blocks": [
                        {
                            "type": "chart",
                            "kind": "line",
                            "title": "趋势",
                            "data": "trend.csv",
                            "x": "date",
                            "series": [
                                {"field": "gaap", "name": "GAAP", "fmt": "num2"}
                            ],
                            "toggle": True,
                        },
                        {
                            "type": "table",
                            "title": "分段",
                            "data": "seg.csv",
                            "sort": "-uid",
                            "top_n": 5,
                            "columns": [
                                {"field": "seg"},
                                {"field": "uid", "fmt": "int"},
                                {"field": "delta", "fmt": "pct2+", "color": "sign"},
                            ],
                        },
                    ],
                },
                {
                    "type": "chart",
                    "kind": "pie",
                    "data": "seg.csv",
                    "x": "seg",
                    "series": ["uid"],
                },
                {
                    "type": "drill_table",
                    "data": "boards.json",
                    "select": "rows",
                    "key": "cid",
                    "columns": [{"field": "name"}, {"field": "w43", "fmt": "num2"}],
                    "children": {
                        "data": "uid.csv",
                        "join": "cid",
                        "columns": [{"field": "uid"}, {"field": "w43", "fmt": "num2"}],
                    },
                },
                {
                    "type": "heatmap_table",
                    "data": "ret.csv",
                    "label_field": "cohort",
                    "row_label": "cohort",
                    "value_fields": ["w1", "w2"],
                    "fmt": "pct1",
                    "note": "留存 = 回访 / 入组",
                },
            ],
        },
        {
            "title": "事件",
            "type": "timeline",
            "blocks": [
                {
                    "type": "timeline",
                    "events": [
                        {"date": "2026-10-05", "title": "活动", "kind": "promo"},
                        {"date": "2026-10-01", "title": "升级", "kind": "product"},
                    ],
                }
            ],
        },
    ]


def _build_blocks_html(tmp_path: Path) -> str:
    _write_block_data(tmp_path)
    sections_file = tmp_path / "sections.json"
    sections_file.write_text(json.dumps(_blocks_sections()), encoding="utf-8")
    sections = builder.load_sections([str(sections_file)])
    return builder.build_report(sections, report_title="T")


def test_format_value_rounds_half_up_and_normalises_negative_zero() -> None:
    assert blocks.format_value(2.675, "num2")[0] == "2.68"
    assert blocks.format_value(-0.000726, "num2") == ("0.00", 0.0)
    assert blocks.format_value(-0.000726, "pct2+")[0] == "0.00%"
    assert blocks.format_value(1.5, "pct2+")[0] == "+1.50%"
    assert blocks.format_value(-1.666, "pct2+")[0] == "-1.67%"
    assert blocks.format_value(0.0123, "ratio")[0] == "1.23%"
    assert blocks.format_value(1234567, "int")[0] == "1,234,567"
    assert blocks.format_value(None, "num2")[0] == "—"
    with pytest.raises(blocks.BlockError):
        blocks.format_value(1, "nope")


def test_sign_colour_follows_the_rounded_value() -> None:
    _, rounded = blocks.format_value(-0.000726, "num2")
    assert blocks.sign_class(rounded, "sign") == ""  # displays 0.00: no colour
    assert blocks.sign_class(1.0, "sign") == "rp-pos"  # rise: green
    assert blocks.sign_class(-1.0, "sign") == "rp-neg"  # decline: blue
    assert blocks.sign_class(1.0, "sign_inv") == "rp-bad"  # cost rise: red
    assert blocks.sign_class(-1.0, "sign_inv") == "rp-pos"
    assert blocks.sign_class(None, "key") == "rp-key"


def test_rich_text_allows_only_the_colour_markup() -> None:
    out = blocks.rich_text("**a** {+b} {-c} <script>x</script>")
    assert '<span class="rp-pos">+b</span>' in out  # the sign is kept
    assert '<span class="rp-neg">-c</span>' in out and "rp-key" in out
    assert "<script>" not in out and "&lt;script&gt;" in out
    legacy = blocks.rich_text('<span class="text-red-500 font-bold">-3%</span>')
    assert legacy == '<span class="rp-neg">-3%</span>'


def test_blocks_render_layout_spec_structures(tmp_path) -> None:
    html = _build_blocks_html(tmp_path)
    # hero, numbered section heads, card per block, KPI band, 2-col grid
    assert 'class="rp-hero"' in html and "rp-hero-sub" not in html
    assert '<span class="rp-sec-no">01</span>' in html
    assert '<span class="rp-sec-no">02</span>' in html
    assert (
        'class="rp-card rp-stack"' in html
        and '<div class="rp-card-h">分段</div>' in html
    )
    assert 'class="rp-kpi-band"' in html and 'class="rp-kpi k1"' in html
    assert 'class="rp-grid-2"' in html
    assert "switchTask" not in html  # sections are stacked, not tabs
    assert "其他（4 项）" in html  # 9 rows, top_n 5
    assert "rp-pos" in html and "rp-neg" in html
    assert 'class="bi-timeline"' in html
    assert html.index("升级") < html.index("活动")  # timeline sorted ascending
    # pie with 9 categories (>6): merged tail
    assert "其他" in html
    # the page is offline and the block output uses classes that ship with it
    assert audit_clean(html)


def audit_clean(html: str) -> bool:
    assert builder.audit_external_resources(html) == []
    classes, _ = builder.audit_class_coverage(html)
    assert classes == [], classes
    return True


def test_theme_blocks_hero_stackbar_and_table_tools(tmp_path) -> None:
    _write_block_data(tmp_path)
    rows = [{"name": f"c{i}", "kind": "A" if i % 2 else "B", "v": i} for i in range(12)]
    sections = [
        {
            "type": "hero",
            "blocks": [
                {
                    "type": "kpi",
                    "items": [
                        {"label": "客户数", "value": 93, "fmt": "int", "unit": "户"},
                        {
                            "label": "降幅",
                            "value": -8.6,
                            "fmt": "num1",
                            "foot": "占 83.9%",
                        },
                    ],
                }
            ],
        },
        {
            "title": "构成",
            "subtitle": "按类型",
            "blocks": [
                {
                    "type": "grid",
                    "cols": "2:1",
                    "blocks": [
                        {
                            "type": "table",
                            "title": "明细",
                            "rows": rows,
                            "rank": True,
                            "search": True,
                            "columns": [
                                {"field": "name"},
                                {"field": "kind", "tag": {"A": "teal"}},
                                {"field": "v", "fmt": "int"},
                            ],
                        },
                        {
                            "type": "stackbar",
                            "title": "类型构成",
                            "rows": [{"t": "A", "n": 6}, {"t": "B", "n": 6}],
                            "x": "t",
                            "value": "n",
                            "unit": "户",
                        },
                    ],
                },
            ],
        },
    ]
    html = builder.build_report(
        builder.load_sections(_write_sections(tmp_path, sections)), report_title="T"
    )
    hero = html[html.index('class="rp-hero"') : html.index('class="rp-sec"')]
    assert "rp-kpi-band" in hero and "<small>户</small>" in hero and "占 83.9%" in hero
    assert '<span class="rp-sec-no">01</span>' in html  # hero is not numbered
    assert '<span class="rp-sec-sub">按类型</span>' in html
    assert 'class="rp-grid-2-1"' in html
    # 12 rows > LONG_TABLE_ROWS: dark sticky header, click-sort, scroll container
    assert 'class="rp-table zebra dark"' in html and "__biSortTable(this)" in html
    assert "rp-table-wrap scroll" in html and 'class="rp-search"' in html
    assert '<td class="rp-rk">1</td>' in html and "rp-tag rp-tag-teal" in html
    assert 'class="rp-stackbar"' in html and "width:50.00%" in html
    assert audit_clean(html)


def _write_sections(tmp_path: Path, sections: list) -> list:
    path = tmp_path / "theme_sections.json"
    path.write_text(json.dumps(sections, ensure_ascii=False), encoding="utf-8")
    return [str(path)]


def test_chart_option_follows_the_theme(tmp_path) -> None:
    _write_block_data(tmp_path)
    ctx = blocks.BlockContext(tmp_path)
    option, _, _ = blocks.build_chart_option(
        ctx,
        {
            "kind": "bar",
            "data": "trend.csv",
            "x": "date",
            "series": [
                {"field": "gaap", "fmt": "num1"},
                {"field": "gaap", "name": "环比", "type": "line", "axis": "right"},
            ],
            "highlight": "last",
        },
    )
    assert isinstance(option["yAxis"], list) and option["series"][1]["yAxisIndex"] == 1
    bars = option["series"][0]["data"]
    assert bars[0]["label"]["formatter"] == "10.5"  # pre-formatted data labels
    assert bars[-1]["itemStyle"]["color"]["type"] == "linear"  # highlighted bar
    assert option["xAxis"]["axisTick"] == {"show": False}
    script = blocks.render_chart(
        ctx, {"kind": "line", "data": "trend.csv", "x": "date"}
    )
    assert 'document.getElementById("chart_s0_' in script


def test_blocks_charts_have_render_paths_and_fallback_tables(tmp_path) -> None:
    html = _build_blocks_html(tmp_path)
    violations, checked = builder.audit_chart_rendering(html)
    # two charts; each also owns a same-data table container
    assert checked >= 2 and violations == []
    assert "echarts.init" in html and "__qwenpawDataFallback" in html
    # chart/table toggle was requested on the first chart only
    assert html.count("data-chart-toggle") >= 2
    assert "-table" in html


def test_drill_toggles_resolve_to_rows(tmp_path) -> None:
    html = _build_blocks_html(tmp_path)
    violations, checked = builder.audit_drill_refs(html)
    assert checked == 2 and violations == []
    dangling = html.replace('id="drill_s0_', 'id="gone_s0_')
    violations, _ = builder.audit_drill_refs(dangling)
    assert violations


def test_external_audit_flags_cdn_references() -> None:
    clean = "<html><head><style>a{}</style></head><body><script>var u='http://x'</script></body></html>"
    assert builder.audit_external_resources(clean) == []
    dirty = clean.replace(
        "</head>", '<link href="https://cdn.example.com/x.css" rel="stylesheet"></head>'
    )
    assert builder.audit_external_resources(dirty)
    dirty_url = clean.replace("a{}", "a{background:url(https://cdn.example.com/f.png)}")
    assert builder.audit_external_resources(dirty_url)


def test_content_lint_warns_on_process_markers() -> None:
    page = "<html><body><p>QC PASS，本版新增 v2 变化，SQL 取数</p></body></html>"
    assert builder.lint_report_content(page)
    assert (
        builder.lint_report_content(
            "<html><body><p>净 GAAP 环比 +1.2%</p></body></html>"
        )
        == []
    )


def _run_main(monkeypatch, sections: Path, out: Path, *extra: str) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "report_builder.py",
            "--sections",
            str(sections),
            "--output",
            str(out),
            *extra,
        ],
    )
    builder.main()


def test_main_builds_blocks_report_fully_offline(tmp_path, monkeypatch, capsys) -> None:
    _write_block_data(tmp_path)
    sections = tmp_path / "sections.json"
    sections.write_text(json.dumps(_blocks_sections()), encoding="utf-8")
    out = tmp_path / "out.html"
    _run_main(monkeypatch, sections, out)

    text = out.read_text(encoding="utf-8")
    printed = capsys.readouterr().out
    assert "外链审计: PASS" in printed
    assert "external: PASS" in text and "charts: PASS" in text
    assert builder.audit_external_resources(text) == []


def test_overall_summary_flag_is_ignored(tmp_path, monkeypatch, capsys) -> None:
    _write_block_data(tmp_path)
    sections = tmp_path / "sections.json"
    sections.write_text(json.dumps(_blocks_sections()), encoding="utf-8")
    out = tmp_path / "out.html"
    _run_main(monkeypatch, sections, out, "--overall-summary", "重复的结论")
    assert "--overall-summary 已废弃" in capsys.readouterr().out
    assert "重复的结论" not in out.read_text(encoding="utf-8")


def test_main_fails_when_template_pulls_external_css(
    tmp_path, monkeypatch, capsys
) -> None:
    _write_block_data(tmp_path)
    sections = tmp_path / "sections.json"
    sections.write_text(json.dumps(_blocks_sections()), encoding="utf-8")
    template = tmp_path / "cdn_template.html"
    template.write_text(
        "<html><head><link href='https://cdn.example.com/t.css' rel='stylesheet'></head>"
        "<body>{% for s in sections %}{{ s.html_fragment }}{% endfor %}</body></html>",
        encoding="utf-8",
    )
    out = tmp_path / "out.html"
    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch, sections, out, "--template", str(template))
    assert exc.value.code == 2
    assert "cdn.example.com" in capsys.readouterr().out

    _run_main(
        monkeypatch, sections, out, "--template", str(template), "--allow-external"
    )
    assert "external: FAIL" in out.read_text(encoding="utf-8")


def test_main_reports_bad_block_specs(tmp_path, monkeypatch, capsys) -> None:
    sections = tmp_path / "sections.json"
    sections.write_text(
        json.dumps(
            [
                {
                    "title": "x",
                    "blocks": [
                        {
                            "type": "kpi",
                            "items": [{"label": "a", "value": 1, "fmt": "bogus"}],
                        }
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch, sections, tmp_path / "out.html")
    assert exc.value.code == 2
    assert "fmt" in capsys.readouterr().out


def test_data_root_overrides_sections_directory(tmp_path, monkeypatch) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _write_block_data(data)
    sections = tmp_path / "sections.json"
    sections.write_text(json.dumps(_blocks_sections()), encoding="utf-8")
    out = tmp_path / "out.html"
    _run_main(monkeypatch, sections, out, "--data-root", str(data))
    assert "bi-timeline" in out.read_text(encoding="utf-8")
