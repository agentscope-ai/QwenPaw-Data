# -*- coding: utf-8 -*-
from __future__ import annotations

from qwenpaw_data.host.core.algo.biztrace.artifact_links import (
    ARTIFACT_HREF_PREFIX,
    ArtifactLinker,
    artifact_href,
)

SESSION = "ses_DaNq42ne"
REPORT = "maas_sfm_agent_gaap_30d_trend_report_v3.html"
DATASET = "data/processed/dau.csv"


def _linker(
    files: list[str] | None = None,
    *,
    session_id: str = SESSION,
) -> ArtifactLinker:
    listed = files if files is not None else [REPORT, DATASET]
    return ArtifactLinker(
        session_id=session_id,
        lister=lambda: [{"rel_path": path} for path in listed],
    )


def test_three_mention_forms_resolve() -> None:
    linker = _linker()
    href = artifact_href(REPORT)

    session_path = f"artifacts/{SESSION}/{REPORT}"
    body = linker.link(f"交付物 {session_path}")
    assert body == f"交付物 [{session_path}]({href})"

    absolute = f"/workspace/artifacts/{SESSION}/{REPORT}"
    body = linker.link(f"交付物 `{absolute}`")
    assert body == f"交付物 [{absolute}]({href})"

    body = linker.link(f"交付物 {REPORT}")
    assert body == f"交付物 [{REPORT}]({href})"


def test_backticked_path_is_unwrapped() -> None:
    linker = _linker()
    mention = f"artifacts/{SESSION}/{REPORT}"
    body = linker.link(f"交付物 `{mention}`")
    assert f"`{mention}`" not in body
    assert body == f"交付物 [{mention}]({artifact_href(REPORT)})"


def test_relative_workspace_path_resolves() -> None:
    linker = _linker()
    body = linker.link(f"结果在 {DATASET}")
    assert body == f"结果在 [{DATASET}]({artifact_href(DATASET)})"


def test_unknown_path_is_left_verbatim() -> None:
    linker = _linker()
    source = f"见 artifacts/{SESSION}/missing.html"
    assert linker.link(source) == source


def test_fenced_code_is_not_linked() -> None:
    linker = _linker()
    source = f"```\n{REPORT}\n```"
    assert linker.link(source) == source


def test_ambiguous_basename_is_not_linked() -> None:
    linker = _linker(["a/report.html", "b/report.html"])
    source = "见 report.html"
    assert linker.link(source) == source
    body = linker.link("见 a/report.html")
    assert body == f"见 [a/report.html]({artifact_href('a/report.html')})"


def test_existing_markdown_link_is_not_nested() -> None:
    linker = _linker()
    source = f"[x](https://example.test/{REPORT})"
    assert linker.link(source) == source


def test_invalidate_rereads_the_listing() -> None:
    files = ["old.html"]
    linker = ArtifactLinker(
        session_id=SESSION,
        lister=lambda: [{"rel_path": path} for path in files],
    )
    assert linker.link("见 old.html") == f"见 [old.html]({artifact_href('old.html')})"

    files[:] = ["new.html"]
    assert linker.link("见 new.html") == "见 new.html"

    linker.invalidate()
    assert linker.link("见 new.html") == f"见 [new.html]({artifact_href('new.html')})"
    assert linker.link("见 old.html") == "见 old.html"


def test_href_is_a_fragment_sentinel() -> None:
    assert artifact_href(REPORT).startswith(ARTIFACT_HREF_PREFIX)
    assert artifact_href("data/processed/dau.csv") == "#artifact:data/processed/dau.csv"


def test_host_absolute_path_in_backticks_resolves() -> None:
    report = "DemoApp_2025年12月_DAU访问趋势分析报告_v1.2.html"
    session_id = "ses-949d3bf1-2d08-4fd4-98bc-b623e8b12dbc"
    linker = _linker([report, "result.yaml", "plan.yaml"], session_id=session_id)
    absolute = (
        "/Users/alice/.qwenpaw-data/fb48c81f2e8741a9/50f03f6cb15e4683/"
        f"859ecb5b9449428f/default/workspace/artifacts/{session_id}/{report}"
    )
    body = linker.link(f"最终报告\n`{absolute}`\n另见 result.yaml 和 plan.yaml")
    assert f"[{absolute}]({artifact_href(report)})" in body
    assert f"[result.yaml]({artifact_href('result.yaml')})" in body
    assert f"[plan.yaml]({artifact_href('plan.yaml')})" in body


def test_wrapped_backticked_absolute_path_resolves() -> None:
    report = "DemoApp_2025年12月_DAU访问趋势分析报告_v1.2.html"
    session_id = "ses-949d3bf1-2d08-4fd4-98bc-b623e8b12dbc"
    linker = _linker([report], session_id=session_id)
    wrapped = (
        f"`/Users/alice/.qwenpaw-data/default/workspace/artifacts/{session_id}/\n{report}`"
    )
    body = linker.link(f"最终报告\n{wrapped}")
    assert artifact_href(report) in body
    assert f"`{report}`" not in body


def test_absolute_path_falls_back_to_unique_basename() -> None:
    linker = _linker([REPORT])
    absolute = f"/tmp/other-root/artifacts/ses_other/{REPORT}"
    body = linker.link(f"`{absolute}`")
    assert body == f"[{absolute}]({artifact_href(REPORT)})"
