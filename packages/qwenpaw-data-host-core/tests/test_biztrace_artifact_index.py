# -*- coding: utf-8 -*-
from __future__ import annotations

from qwenpaw_data.host.core.algo.biztrace.pipeline import ArtifactFileIndex


def test_artifact_index_scopes_files_by_tool_result_seq() -> None:
    index = ArtifactFileIndex()

    index.note_delta({"a.csv": "out/a.csv"})
    index.bind_pending("c1")
    index.note_delta({"b.csv": "out/b.csv", "a.csv": "out/a2.csv"})
    index.bind_pending("c2")
    index.assign_seq("c1", 10)
    index.assign_seq("c2", 20)

    # Earlier production survives a later rewrite when filtering by seq.
    assert index.files_in(10, 10) == {"a.csv": "out/a.csv"}
    assert index.files_in(20, 20) == {"a.csv": "out/a2.csv", "b.csv": "out/b.csv"}
    # Same name in range: last in-range writer wins.
    assert index.files_in(10, 20) == {"a.csv": "out/a2.csv", "b.csv": "out/b.csv"}
    assert index.files_in(11, 19) == {}


def test_artifact_index_ignores_empty_delta_and_unknown_call() -> None:
    index = ArtifactFileIndex()

    index.bind_pending("c1")
    index.assign_seq("c1", 1)
    index.assign_seq(None, 2)
    index.assign_seq("missing", 3)

    assert index.files_in(1, 3) == {}
