# -*- coding: utf-8 -*-
# pylint: disable=missing-class-docstring,missing-function-docstring
"""测试 gold_parser.section_splitter 的层级保留解析。"""

from __future__ import annotations

from pathlib import Path

import pytest

from gold_parser.section_splitter import ReportSectionSplitter
from stub_agent_runner import StubAgentRunner


@pytest.fixture()
def sample_md(tmp_path: Path) -> Path:
    text = """# 应用访问→创建→发布→调用漏斗

各环节转化率 15% / 50% / 31%，核心卡点在发布环节。
建议优化发布流程，增加发布引导。

## 百万活跃项目 action 汇总

项目 A 转化 40%，项目 B 转化 25%。
建议：对齐发布流程负责人 @张三，排期下周完成。

## 国际版 mau 增但转化低

国际版 mau 增长 20% 但转化率仅 5%，增量与转化背离。
"""
    p = tmp_path / "product_a_goal_status.md"
    p.write_text(text, encoding="utf-8")
    return p


def test_split_markdown_preserves_title_subtitle_content(sample_md: Path):
    report = ReportSectionSplitter().split(sample_md)
    assert report.report_id == "product_a_goal_status"
    assert len(report.sections) == 1
    section = report.sections[0]
    assert section.title.startswith("应用访问")
    assert "各环节转化率" in section.raw_text
    assert len(section.subsections) == 2
    assert "百万活跃" in section.subsections[0].title
    assert "项目 A 转化 40%" in section.subsections[0].content
    assert "国际版" in section.subsections[1].title


def test_section_ids_are_slugified(sample_md: Path):
    report = ReportSectionSplitter().split(sample_md)
    for s in report.sections:
        assert s.section_id
        assert " " not in s.section_id


def test_regex_buckets_are_not_populated(sample_md: Path):
    report = ReportSectionSplitter().split(sample_md)
    section = report.sections[0]
    assert section.data_points == []
    assert section.actions == []
    assert section.out_of_scope == []
    assert section.section_types == []
    assert section.metadata["parse_mode"] == "hierarchy"


def test_unsupported_suffix_returns_empty(tmp_path: Path):
    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF-1.4")
    report = ReportSectionSplitter().split(p)
    assert report.sections == []


def test_min_section_text_filters_thin_blocks(tmp_path: Path):
    p = tmp_path / "thin.md"
    p.write_text("# A\nx\n# B\ny\n", encoding="utf-8")
    report = ReportSectionSplitter().split(p)
    # 'x' and 'y' alone are below _MIN_SECTION_TEXT -> filtered
    assert report.sections == []


def test_split_html_renders_tables_as_markdown(tmp_path: Path):
    p = tmp_path / "monthly.html"
    p.write_text(
        """
        <html><body>
          <h1>月度经营报告</h1>
          <p>核心指标已经完成复现，下面是原报告表格。</p>
          <table>
            <tr><th>指标</th><th>数值</th></tr>
            <tr><td>活跃用户</td><td>1,000</td></tr>
            <tr><td>完成率</td><td>25%</td></tr>
          </table>
        </body></html>
        """,
        encoding="utf-8",
    )

    report = ReportSectionSplitter().split(p)

    assert report.sections
    text = report.sections[0].raw_text
    assert "<table" not in text
    assert "| 指标 | 数值 |" in text
    assert "| 活跃用户 | 1,000 |" in text


def test_split_html_ignores_style_and_script_content(tmp_path: Path):
    p = tmp_path / "styled.html"
    p.write_text(
        """
        <html>
          <head>
            <style>
              :root { --red:#C62828; }
              body { margin:0; }
            </style>
            <script>window.__debug = true;</script>
          </head>
          <body>
            <h1>MaaS 模型客户月报</h1>
            <p>本月活跃客户 1,000，收入完成率 25%。</p>
          </body>
        </html>
        """,
        encoding="utf-8",
    )

    report = ReportSectionSplitter().split(p)

    assert report.sections
    text = report.sections[0].raw_text
    assert ":root" not in text
    assert "window.__debug" not in text
    assert "本月活跃客户 1,000" in text


def test_split_html_recognizes_embedded_markdown_headings(tmp_path: Path):
    p = tmp_path / "markdownish.html"
    p.write_text(
        """
        <html><body>
          <div>
            # MaaS 模型(sfm) 客户月报（202604–202607）
            ## 一、整体收入分布（7月 202607 · 新客/老客 × 板块 · 国内/国际）
            ### a）7月新客 / 老客收入分布
            GAAP 合计 63,484.53 万元。
            ## 二、留存分析
            留存分析正文足够长。
            ## 三、客户下降榜单
            客户下降榜单正文足够长。
            ## 四、客户上涨榜单
            客户上涨榜单正文足够长。
            ## 五、附录：口径与数据源说明（9 项）
            附录说明正文足够长。
          </div>
        </body></html>
        """,
        encoding="utf-8",
    )

    report = ReportSectionSplitter().split(p)

    assert report.title == "MaaS 模型(sfm) 客户月报（202604–202607）"
    assert len(report.sections) == 1
    assert [sub.title for sub in report.sections[0].subsections] == [
        "一、整体收入分布（7月 202607 · 新客/老客 × 板块 · 国内/国际）",
        "二、留存分析",
        "三、客户下降榜单",
        "四、客户上涨榜单",
        "五、附录：口径与数据源说明（9 项）",
    ]


def test_split_html_attaches_header_kicker_and_keeps_h2_chapters(tmp_path: Path):
    p = tmp_path / "maas_header.html"
    p.write_text(
        """
        <html><body>
          <header class="rpt-h">
            <div class="kicker">Product A Ops · MaaS Customer Monthly · Model (sfm)</div>
            <h1><span class="grad">MaaS 模型(sfm) 客户月报</span>（202604–202607）</h1>
            <div class="meta">商业化新口径 · 月度 202604–202607</div>
          </header>
          <h2 class="sec">一、整体收入分布（7月 202607 · 新客/老客 × 板块 · 国内/国际）</h2>
          <p>整体收入段落足够长，作为第一个业务章节。</p>
          <h2 class="sec">二、留存分析（留存总览 · 区间总量）</h2>
          <p>留存分析段落足够长，作为第二个业务章节。</p>
        </body></html>
        """,
        encoding="utf-8",
    )

    report = ReportSectionSplitter().split(p)

    assert report.title == "MaaS 模型(sfm) 客户月报（202604–202607）"
    assert [section.title for section in report.sections] == [
        "MaaS 模型(sfm) 客户月报（202604–202607）",
    ]
    assert [sub.title for sub in report.sections[0].subsections] == [
        "一、整体收入分布（7月 202607 · 新客/老客 × 板块 · 国内/国际）",
        "二、留存分析（留存总览 · 区间总量）",
    ]
    assert "Product A Ops" in report.sections[0].raw_text


# ---- multimodal: docx inline image extraction + vision pass ----

_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
    b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _build_docx_with_image(tmp_path: Path) -> Path:
    docx = pytest.importorskip("docx")
    out = tmp_path / "gold_with_img.docx"
    doc = docx.Document()
    doc.add_heading("10月目标进展", level=1)
    doc.add_paragraph("双百应用目标进展 action：")
    img_path = tmp_path / "tiny.png"
    img_path.write_bytes(_TINY_PNG)
    doc.add_picture(str(img_path))
    doc.add_paragraph("月使用用户数 88 万，目标 100 万。")
    doc.save(str(out))
    return out


def test_splitter_extracts_image_paths_and_descriptions(tmp_path: Path):
    docx_path = _build_docx_with_image(tmp_path)
    canned = "图类型：对比表。双百应用 17/100，完成率 17%。结论：缺口大。"
    runner = StubAgentRunner(responses={"看这张来自金标报告的图": canned})
    media = tmp_path / "media"
    splitter = ReportSectionSplitter(agent_runner=runner, media_dir=media)

    report = splitter.split(docx_path)

    assert report.sections
    section = report.sections[0]
    assert section.image_paths, "image_paths should be populated"
    assert len(section.image_descriptions) == len(section.image_paths)
    # description inlined into raw_text, marker gone
    assert "17/100" in section.raw_text
    assert "[图1]" not in section.raw_text
    # image file actually written to media_dir
    assert Path(section.image_paths[0]).exists()
    # runner received the image
    assert any(c["images"] for c in runner._calls)


def test_splitter_without_runner_still_extracts_paths(tmp_path: Path):
    docx_path = _build_docx_with_image(tmp_path)
    media = tmp_path / "media"
    splitter = ReportSectionSplitter(agent_runner=None, media_dir=media)

    report = splitter.split(docx_path)

    assert report.sections
    section = report.sections[0]
    assert section.image_paths, "paths still extracted without a runner"
    assert section.image_descriptions == []
    # marker left in place (no vision pass to replace it)
    assert "[图1]" in section.raw_text


def test_splitter_without_media_dir_skips_image_extraction(tmp_path: Path):
    _build_docx_with_image(tmp_path)  # ensure docx is buildable; not used
    # No media_dir -> images can't be dumped -> no image blocks emitted.
    md = tmp_path / "x.md"
    md.write_text("# t\nbody text long enough to pass the min filter here\n")
    report = ReportSectionSplitter(agent_runner=None, media_dir=None).split(md)
    assert report.sections
    assert report.sections[0].image_paths == []


def test_split_markdown_preserves_deeper_heading_tree(tmp_path: Path):
    p = tmp_path / "nested.md"
    p.write_text(
        "# 12月目标进展\n"
        "总览内容足够长，避免被最小长度过滤。\n"
        "## 百万活跃\n"
        "百万活跃内容足够长。\n"
        "### 模型体验中心\n"
        "模型体验中心子内容足够长。\n"
        "#### 调用链路\n"
        "调用链路更深层内容足够长。\n",
        encoding="utf-8",
    )

    report = ReportSectionSplitter().split(p)

    assert len(report.sections) == 1
    top = report.sections[0]
    assert top.title == "12月目标进展"
    assert top.subsections[0].title == "百万活跃"
    level3 = top.subsections[0].children[0]
    assert level3.title == "模型体验中心"
    assert level3.level == 3
    level4 = level3.children[0]
    assert level4.title == "调用链路"
    assert level4.level == 4
    assert "#### 调用链路" in top.raw_text
