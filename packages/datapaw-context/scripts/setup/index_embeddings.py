#!/usr/bin/env python3
"""为 Metric / Dimension / Column / Event / Entity 写入语义向量。

用法：

    # 全部三种 label 都写
    python scripts/12_index_embeddings.py

    # 只写 metric + dimension
    python scripts/12_index_embeddings.py --scope metric,dimension

    # EMBED_DIM 改了或者怀疑 hash 失灵，强制全部重算
    python scripts/12_index_embeddings.py --reset

幂等：每个节点的 embedding_hash = sha1(model_name | canonical_text)；和当前一致就跳过。
Column 全量参与（无注释时也按表.列与类型生成文本）。
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from neo4j import GraphDatabase  # noqa: E402

from context_manager.config import CFG  # noqa: E402
from context_manager.graph.embeddings import SCOPE_ALL, index_embeddings  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scope", default="all",
                   help="逗号分隔：metric / dimension / column / event / entity / all（默认 all）")
    p.add_argument("--batch-size", type=int, default=32, help="每批送 embed() 的文本数")
    p.add_argument("--reset", action="store_true",
                   help="忽略 embedding_hash，全量重算（向量维度变更后用）")
    p.add_argument("--no-ensure-indexes", action="store_true",
                   help="不自动建/重建向量索引（已知索引正常时可跳过）")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = p.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    scope = [s for s in (args.scope or "all").split(",") if s.strip()]
    if not scope:
        scope = list(SCOPE_ALL)

    print(f"[embed] model={CFG.embed_model} dim={CFG.embed_dim}")
    print(f"[embed] scope={scope} reset={args.reset} batch_size={args.batch_size}")

    driver = GraphDatabase.driver(
        CFG.neo4j_uri, auth=(CFG.neo4j_user, CFG.neo4j_password)
    )
    try:
        stats = index_embeddings(
            driver,
            scope=scope,
            batch_size=args.batch_size,
            reset=args.reset,
            ensure_indexes=not args.no_ensure_indexes,
        )
    finally:
        driver.close()

    print()
    print(f"{'label':<12} {'total':>8} {'written':>8} {'skipped':>8} {'unemb':>8} {'empty_txt':>10} {'elapsed':>10}")
    print("-" * 72)
    total_unembedded = 0
    for s in stats:
        print(f"{s.label:<12} {s.total:>8} {s.written:>8} {s.skipped:>8} {s.unembedded:>8} {s.empty_text:>10} {s.elapsed_ms:>9.0f}ms")
        total_unembedded += s.unembedded

    if total_unembedded > 0:
        print()
        print(f"[ERROR] {total_unembedded} nodes across all labels still have no embedding.")
        print("This means semantic search will silently miss those nodes.")
        print("Re-run with --reset if you suspect stale hashes, or check the")
        print("embedding provider (DashScope API key / model availability).")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
