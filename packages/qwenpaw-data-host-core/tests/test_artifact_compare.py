# -*- coding: utf-8 -*-
"""Hand-written FastAPI cases for the generic ``POST /files/compare`` endpoint.

The kernel itself is covered by ``test_compare.py``; these cases pin the HTTP
twin down — the identity/sharing gate, the artifact-dir confinement (escape /
absolute / missing / empty), the ``tolerance`` bound, and the utf-8 guard — plus
one end-to-end verdict so a routing regression cannot slip through silently.

The endpoint never reaches the model, so no builder stub is needed here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from qwenpaw_data.host.core.api.app import create_app
from qwenpaw_data.host.core.db.engine import (
    TEST_TENANT_ID,
    TEST_USER_ID,
    TEST_WORKSPACE_ID,
    get_session_factory,
    reset_engine,
)
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.settings import clear_settings, get_settings
from qwenpaw_data.host.core.stream.hub import reset_hub

TEST_IDENTITY = Identity(
    user_id=TEST_USER_ID,
    tenant_id=TEST_TENANT_ID,
    workspace_id=TEST_WORKSPACE_ID,
)


@pytest.fixture
def client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_test_preferences,
):
    asyncio.run(reset_engine())
    reset_hub()
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    clear_settings()
    app = create_app()
    with TestClient(app) as test_client:
        asyncio.run(seed_test_preferences(get_session_factory()))
        yield test_client


def _new_session(client: TestClient, title: str = "compare") -> tuple[str, Path]:
    """Create a session and return ``(session_id, artifact_dir)`` on disk."""
    body = client.post("/api/sessions", json={"title": title}).json()["session"]
    session_id = body["id"]
    paths = UserAgentPaths.from_identity(
        home=get_settings().paths.home,
        identity=TEST_IDENTITY,
        agent_id=body["agent_id"],
        session_id=session_id,
    )
    paths.artifact_dir.mkdir(parents=True, exist_ok=True)
    return session_id, paths.artifact_dir


def _compare(client: TestClient, session_id: str, payload: dict[str, Any]):
    return client.post(
        f"/api/sessions/{session_id}/files/compare",
        json=payload,
    )


def test_upload_rejects_linked_directory(client, tmp_path):
    session_id, folder = _new_session(client)
    outside = tmp_path / "outside-uploads"
    outside.mkdir()
    (folder / "uploads").symlink_to(outside, target_is_directory=True)
    response = client.post(f"/api/sessions/{session_id}/files/upload",
                           files={"file": ("data.txt", b"private")})
    assert response.status_code == 403
    assert list(outside.iterdir()) == []


def test_upload_skips_dangling_links_and_preserves_existing_files(client, tmp_path):
    session_id, folder = _new_session(client)
    uploads = folder / "uploads"
    uploads.mkdir()
    outside = tmp_path / "must-not-be-created.txt"
    (uploads / "data.txt").symlink_to(outside)
    (uploads / "data-1.txt").write_bytes(b"existing")
    response = client.post(f"/api/sessions/{session_id}/files/upload",
                           files={"file": ("data.txt", b"new")})
    assert response.status_code == 200, response.text
    assert response.json()["path"] == "uploads/data-2.txt"
    assert (uploads / "data-2.txt").read_bytes() == b"new"
    assert (uploads / "data-1.txt").read_bytes() == b"existing"
    assert (uploads / "data.txt").is_symlink()
    assert not outside.exists()


def test_html_difference_maps_to_exact_original_text_not_script_or_duplicate(
    client: TestClient,
) -> None:
    from html import unescape

    session_id, folder = _new_session(client)
    reference = "<html><head><script>999</script></head><body><h1>收入😀</h1><p>收入 100 元</p><p>费用 60 元</p><p>其余 50 元</p></body></html>"
    target = reference.replace("费用 60", "费用 &#53;&#48;")
    (folder / "reference.html").write_text(reference)
    (folder / "target.html").write_text(target)
    response = _compare(
        client,
        session_id,
        {"reference_path": "reference.html", "target_path": "target.html"},
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["numeric_differences"]) == 1
    span = body["target_html_spans"][0]
    assert span["id"] == "diff-0"
    assert target[span["start"] : span["end"]] == "&#53;&#48;"
    assert (
        unescape(target[span["start"] : span["end"]])
        == body["numeric_differences"][0]["target"]
    )


def test_html_source_mapping_preserves_existing_markdown_view() -> None:
    from qwenpaw_data.host.core.utils.html_text import html_comparison_text, html_to_markdown

    samples = [
        "<p> A <b>42</b> &amp; &#50; </p><h2> Heading </h2><div>10<br>20</div>",
        "<table><tr><th>A|B</th><th>C</th></tr><tr><td>12</td><td>34</td></tr></table>",
        "<head><style>999</style></head><p>😀 100</p><svg><text>123</text></svg><p> 50 </p>",
    ]
    for source in samples:
        text, mapping = html_comparison_text(source)
        assert text == html_to_markdown(source)
        assert len(mapping) == len(text)


def test_compare_returns_neutral_verdict_on_identical_documents(
    client: TestClient,
) -> None:
    session_id, artifact_dir = _new_session(client)
    text = "# 收入报告\n\n本月收入 1000 万元，成本 800 万元，利润 200 万元。\n"
    (artifact_dir / "reference.md").write_text(text, encoding="utf-8")
    (artifact_dir / "target.md").write_text(text, encoding="utf-8")

    resp = _compare(
        client,
        session_id,
        {"target_path": "target.md", "reference_path": "reference.md"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["passed"] is True
    assert data["numeric_differences"] == []
    assert data["numeric_coverage"] == 1.0
    # The endpoint echoes the artifact-relative paths it actually compared.
    assert data["target_path"] == "target.md"
    assert data["reference_path"] == "reference.md"


def test_compare_flags_a_reference_number_the_target_dropped(
    client: TestClient,
) -> None:
    session_id, artifact_dir = _new_session(client)
    (artifact_dir / "reference.md").write_text(
        "# 报告\n\n收入 1000 万元，成本 2000 万元，另有若干说明文字填充填充填充。\n",
        encoding="utf-8",
    )
    (artifact_dir / "target.md").write_text(
        "# 报告\n\n收入 1000 万元，成本未披露，另有若干说明文字填充填充填充填充。\n",
        encoding="utf-8",
    )

    resp = _compare(
        client,
        session_id,
        {"target_path": "target.md", "reference_path": "reference.md"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["passed"] is False
    assert data["numeric_coverage"] == 0.5
    assert len(data["numeric_differences"]) == 1
    dropped = data["numeric_differences"][0]
    assert dropped["reference"] == "2000"
    assert dropped["target"] is None
    assert dropped["matched"] is False


def test_compare_tolerance_widens_the_match_window(client: TestClient) -> None:
    session_id, artifact_dir = _new_session(client)
    (artifact_dir / "reference.md").write_text(
        "# 报告\n\n收入 1000 万元，另有说明文字填充填充填充填充填充填充填充。\n",
        encoding="utf-8",
    )
    (artifact_dir / "target.md").write_text(
        "# 报告\n\n收入 950 万元，另有说明文字填充填充填充填充填充填充填充。\n",
        encoding="utf-8",
    )
    body = {"target_path": "target.md", "reference_path": "reference.md"}

    strict = _compare(client, session_id, body)
    assert strict.status_code == 200, strict.text
    assert strict.json()["passed"] is False

    loose = _compare(client, session_id, {**body, "tolerance": 0.1})
    assert loose.status_code == 200, loose.text
    assert loose.json()["passed"] is True
    assert loose.json()["numeric_differences"] == []


@pytest.mark.parametrize("tolerance", [1.5, -0.1])
def test_compare_rejects_out_of_range_tolerance(
    client: TestClient,
    tolerance: float,
) -> None:
    session_id, artifact_dir = _new_session(client)
    (artifact_dir / "reference.md").write_text(
        "# 报告\n\n收入 1000 万元。\n", encoding="utf-8"
    )
    (artifact_dir / "target.md").write_text(
        "# 报告\n\n收入 1000 万元。\n", encoding="utf-8"
    )

    resp = _compare(
        client,
        session_id,
        {
            "target_path": "target.md",
            "reference_path": "reference.md",
            "tolerance": tolerance,
        },
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "VALIDATION"


def test_compare_rejects_paths_that_escape_the_artifact_dir(
    client: TestClient,
) -> None:
    session_id, artifact_dir = _new_session(client)
    (artifact_dir / "target.md").write_text(
        "# 报告\n\n收入 1000 万元。\n", encoding="utf-8"
    )

    resp = _compare(
        client,
        session_id,
        {"target_path": "target.md", "reference_path": "../secret.md"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "VALIDATION"


def test_compare_rejects_absolute_paths(client: TestClient) -> None:
    session_id, artifact_dir = _new_session(client)
    (artifact_dir / "target.md").write_text(
        "# 报告\n\n收入 1000 万元。\n", encoding="utf-8"
    )

    resp = _compare(
        client,
        session_id,
        {"target_path": "/etc/passwd", "reference_path": "target.md"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "VALIDATION"


def test_compare_rejects_empty_path(client: TestClient) -> None:
    session_id, artifact_dir = _new_session(client)
    (artifact_dir / "target.md").write_text(
        "# 报告\n\n收入 1000 万元。\n", encoding="utf-8"
    )

    resp = _compare(
        client,
        session_id,
        {"target_path": "   ", "reference_path": "target.md"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "VALIDATION"


def test_compare_missing_file_is_not_found(client: TestClient) -> None:
    session_id, artifact_dir = _new_session(client)
    (artifact_dir / "target.md").write_text(
        "# 报告\n\n收入 1000 万元。\n", encoding="utf-8"
    )

    resp = _compare(
        client,
        session_id,
        {"target_path": "target.md", "reference_path": "does-not-exist.md"},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "NOT_FOUND"


def test_compare_rejects_non_utf8_documents(client: TestClient) -> None:
    session_id, artifact_dir = _new_session(client)
    (artifact_dir / "target.md").write_text(
        "# 报告\n\n收入 1000 万元。\n", encoding="utf-8"
    )
    (artifact_dir / "binary.md").write_bytes(b"\xff\xfe\x00\x01 not utf-8")

    resp = _compare(
        client,
        session_id,
        {"target_path": "target.md", "reference_path": "binary.md"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "VALIDATION"


def test_compare_unknown_session_is_rejected(client: TestClient) -> None:
    # The session gate is identity-scoped: a session id nobody owns under the
    # current identity never reaches the compare kernel.
    resp = _compare(
        client,
        "session-does-not-exist",
        {"target_path": "target.md", "reference_path": "reference.md"},
    )
    assert resp.status_code in {403, 404}, resp.text


def test_compare_recommendations_pass_usage_to_build_model_and_return_pairs(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard for the compare picker's recommendations endpoint.

    ``build_model`` grew a required keyword-only ``usage`` argument on develop;
    the recommendation call must pass it, or the missing-argument ``TypeError``
    is swallowed by the ``except`` and the endpoint silently returns nothing.
    The stub below mirrors that signature (``usage`` is required, no default),
    so reverting the fix makes the fake raise and this test fail. Routing, the
    session gate, the ``session_id`` threading and the same-family validation
    are all exercised over HTTP; only the model itself is faked.
    """
    import qwenpaw_data.host.core.algo.settlement.llm as llm_module
    import qwenpaw_data.host.core.providers.factory as factory_module
    from qwenpaw_data.host.core.api.models.artifact_compare import (
        Recommendation,
        RecommendationList,
    )

    captured: dict[str, Any] = {}

    def _fake_build_model(model, *, usage):  # usage required, like the real one
        captured["usage"] = usage
        return object()

    async def _fake_structured_call(model, messages, schema, *, attempts, timeout):
        return RecommendationList(
            recommendations=[
                Recommendation(
                    key="gt",
                    label="对照 GT",
                    description="把复现稿和原报告对照",
                    reference_path="report_v1.md",
                    target_path="report_v2.md",
                )
            ]
        )

    monkeypatch.setattr(factory_module, "build_model", _fake_build_model)
    monkeypatch.setattr(llm_module, "for_structured_calls", lambda m: m)
    monkeypatch.setattr(llm_module, "structured_call", _fake_structured_call)

    session_id, artifact_dir = _new_session(client)
    (artifact_dir / "report_v1.md").write_text(
        "# 报告\n\n收入 1000 万元。\n", encoding="utf-8"
    )
    (artifact_dir / "report_v2.md").write_text(
        "# 报告\n\n收入 1000 万元。\n", encoding="utf-8"
    )

    resp = client.post(
        f"/api/sessions/{session_id}/files/compare/recommendations",
        json={
            "current_path": "report_v2.md",
            "candidates": [{"path": "report_v1.md", "name": "report_v1.md"}],
        },
    )
    assert resp.status_code == 200, resp.text
    recs = resp.json()["recommendations"]
    assert len(recs) == 1, recs
    assert recs[0]["reference_path"] == "report_v1.md"
    # The target is locked to the previewed document, not what the model echoed.
    assert recs[0]["target_path"] == "report_v2.md"

    usage = captured.get("usage")
    assert usage is not None, "build_model 必须收到 usage 归因上下文"
    assert str(usage.source) == "compare"
    assert usage.session_id == session_id
