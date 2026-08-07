"""CLI: ``python -m context_manager.knowledge``."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = REPO_ROOT.parent / "业务文档" / "Studio材料" / "merged.txt"


def main(argv: list[str] | None = None) -> int:
    from .pipeline import run_doc_ingest

    p = argparse.ArgumentParser(description="Doc → Neo4j knowledge ingest (Pass A/B/C + writes + Pass D cross-graph linking)")
    p.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help="Path to merged.txt or similar corpus",
    )
    p.add_argument(
        "--dataset",
        default=None,
        help="Dataset profile name (default: infer from NEO4J_DATABASE / appdata)",
    )
    p.add_argument(
        "--max-chunks",
        type=int,
        default=0,
        help="Process at most N chunks (0 = all)",
    )
    p.add_argument(
        "--min-chunk-chars",
        type=int,
        default=3200,
        help="chunk_merged_txt min_chars (default 3200)",
    )
    p.add_argument(
        "--max-chunk-chars",
        type=int,
        default=6000,
        help="chunk_merged_txt max_chars (default 6000)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip ingest_all only; Pass D still reads Neo4j + runs LLM unless --skip-llm",
    )
    p.add_argument(
        "--skip-llm",
        action="store_true",
        help="Bypass every LLM call (Pass A/B/C/D). Useful for plumbing/structure checks.",
    )
    p.add_argument(
        "--pass-d-apply",
        action="store_true",
        help="With --dry-run: still MERGE Pass D edges (endpoints must exist in Neo4j)",
    )
    p.add_argument(
        "--pass-d-entity-batch",
        type=int,
        default=0,
        metavar="N",
        help="Pass D: at most N ent: clusters per LLM call (0 = single call with all)",
    )
    p.add_argument(
        "--pass-d-max-rounds",
        type=int,
        default=4,
        metavar="R",
        help="Pass D: max outer rounds when multiple ent batches (default 4)",
    )
    p.add_argument(
        "--pass-c-cluster-batch",
        type=int,
        default=20,
        metavar="N",
        help="Pass C: at most N clusters per LLM slice (0 = one slice with all clusters)",
    )
    p.add_argument(
        "--pass-c-max-rounds",
        type=int,
        default=1,
        metavar="R",
        help="Pass C: outer refinement rounds (1=only array rounds; 2+=prior kg summary for cross-slice links)",
    )
    args = p.parse_args(argv)

    if not args.source.exists():
        print(f"error: source not found: {args.source}", file=sys.stderr)
        return 2

    out = run_doc_ingest(
        None,
        source_path=args.source,
        dataset=args.dataset,
        max_chunks=args.max_chunks,
        dry_run=args.dry_run,
        skip_llm=args.skip_llm,
        pass_d_apply_edges=args.pass_d_apply,
        pass_d_entity_batch_size=args.pass_d_entity_batch,
        pass_d_max_rounds=args.pass_d_max_rounds,
        pass_c_cluster_batch_size=args.pass_c_cluster_batch,
        pass_c_max_outer_rounds=args.pass_c_max_rounds,
        chunk_min_chars=args.min_chunk_chars,
        chunk_max_chars=args.max_chunk_chars,
    )
    print(out)
    return 0
