"""Orchestrate chunk → Pass A → B → C → Neo4j writes + markdown report."""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from qwenpaw_data.context.paths import knowledge_ingest_cache_dir, knowledge_ingest_report_path

from ..config import CFG
from ..graph.profile import profile_for_dataset
from ..utils import get_logger
from .chunker import chunk_merged_txt
from .extractor import extract_chunk
from .normalize import (
    collect_surface_records,
    run_pass_b,
    run_pass_c_batched,
)

if TYPE_CHECKING:
    from .progress import IngestProgress

log = get_logger("knowledge.pipeline")

REPORT_PATH = knowledge_ingest_report_path()
_CACHE_DIR_HINT = knowledge_ingest_cache_dir()


def _ui_entity_rows(row: dict[str, Any], *, limit: int = 260) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ent in (row.get("entities") or [])[:limit]:
        if not isinstance(ent, dict):
            continue
        ev = str(ent.get("evidence_quote") or "").strip()
        if len(ev) > 260:
            ev = ev[:260] + "…"
        al = ent.get("aliases") or []
        aliases: list[str] = []
        if isinstance(al, list):
            for a in al[:18]:
                t = str(a).strip()
                if t:
                    aliases.append(t)
        out.append(
            {
                "surface": str(ent.get("surface") or "").strip(),
                "type": str(ent.get("type") or "").strip(),
                "aliases": aliases,
                "evidence_quote": ev,
            }
        )
    return out


def _ui_event_rows(row: dict[str, Any], *, limit: int = 48) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ev in (row.get("events") or [])[:limit]:
        if not isinstance(ev, dict):
            continue
        desc = str(ev.get("description") or "").strip()
        if len(desc) > 300:
            desc = desc[:300] + "…"
        out.append(
            {
                "name": str(ev.get("name") or "").strip(),
                "type": str(ev.get("type") or "").strip(),
                "date_from": str(ev.get("date_from") or "").strip(),
                "about_surface": str(ev.get("about_surface") or "").strip(),
                "description": desc,
            }
        )
    return out


def _write_report(
    path: Path,
    *,
    source: Path,
    n_chunks: int,
    n_clusters: int,
    n_kg_nodes: int,
    n_kg_rels: int,
    n_pass_d_edges: int,
    n_pass_d_proposed: int,
    pass_d_preview_only: bool,
    rep: Any,
    dry_run: bool,
    pass_d_llm_calls: int = 0,
    pass_d_outer_rounds: int = 0,
    pass_d_entity_batch_size: int = 0,
    pass_c_llm_calls: int = 0,
    pass_c_cluster_batch_size: int = 0,
    pass_c_max_outer_rounds: int = 1,
) -> None:
    counts = rep.count_by() if hasattr(rep, "count_by") else {}
    lines = [
        "# Knowledge ingest report",
        "",
        f"- **source**: `{source}`",
        f"- **chunks**: {n_chunks}",
        f"- **clusters**: {n_clusters}",
        f"- **pass_c kg_topology nodes**: {n_kg_nodes}",
        f"- **pass_c kg_topology relationships**: {n_kg_rels}",
        f"- **pass_c LLM calls** (cluster batches × outer rounds): {pass_c_llm_calls} "
        f"(batch_size={pass_c_cluster_batch_size or 'all'}, outer_rounds={pass_c_max_outer_rounds})",
        f"- **pass_d edges proposed (LLM, all calls)**: {n_pass_d_proposed}",
        f"- **pass_d cross-graph edges applied (MERGE)**: {n_pass_d_edges}",
        f"- **pass_d LLM calls**: {pass_d_llm_calls} (outer rounds × batches; batch_size={pass_d_entity_batch_size or 'all'})",
        f"- **pass_d outer rounds executed**: {pass_d_outer_rounds}",
        f"- **pass_d preview only (no MERGE)**: {pass_d_preview_only}",
        f"- **dry_run**: {dry_run}",
        "",
        "## WriteDecision counts (KnowledgeWriter)",
        "",
        json.dumps(counts, indent=2, ensure_ascii=False),
        "",
        "## Skipped / notes (sample)",
        "",
    ]
    for s in (rep.skipped or [])[:80]:
        lines.append(f"- {s}")
    lines.append("")
    lines.append("## Sample decisions (up to 40)")
    lines.append("")
    for key, lab, dec in (rep.decisions or [])[:40]:
        lines.append(f"- `{key}` ({lab}) → **{dec}**")
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def run_doc_ingest(
    driver: Optional[Any] = None,
    *,
    source_path: Path,
    dataset: Optional[str] = None,
    max_chunks: int = 0,
    dry_run: bool = False,
    skip_llm: bool = False,
    pass_d_apply_edges: bool = False,
    pass_d_entity_batch_size: int = 0,
    pass_d_max_rounds: int = 4,
    pass_c_cluster_batch_size: int = 20,
    pass_c_max_outer_rounds: int = 1,
    chunk_min_chars: int = 3200,
    chunk_max_chars: int = 6000,
    progress: Optional["IngestProgress"] = None,
) -> dict[str, Any]:
    """End-to-end doc → graph ingest.

    ``dry_run`` skips ``ingest_all`` only (no new Entity/Event from this run). Pass D still
    connects to Neo4j, reads MG/TG/KG catalogs, and runs the LLM unless ``skip_llm``.
    Proposed edges are not MERGE'd unless ``pass_d_apply_edges`` is true (meaningful when
    ``dry_run`` and you only want cross-graph Pass D writes).
    ``pass_d_entity_batch_size`` (>0) splits ``ent:`` clusters into batches per LLM call; when more than one batch
    exists, ``pass_d_max_rounds`` outer rounds re-fetch Neo4j catalogs and repeat all batches until a full round
    proposes zero edges or the round cap is hit.
    ``pass_c_cluster_batch_size`` (>0) runs Pass C once per slice of that many clusters and merges ``kg_topology``
    (deduping relationships). Use ``0`` for one slice containing **all** clusters (single batch per outer round).
    ``pass_c_max_outer_rounds`` (>1) repeats the full batch loop: round 1 uses plain JSON arrays per slice; later
    rounds wrap each slice with ``pass_c_prior_topology`` from the merged graph so the model can add cross-slice links.
    """
    from .writer import IngestReport, ingest_all

    profile = profile_for_dataset(dataset)
    db_id = "app_db"
    schema = "public"

    own = False
    work = driver

    try:
        if progress is not None:
            progress.begin_run(
                source_path=str(source_path),
                max_chunks=max_chunks,
                dry_run=dry_run,
                skip_llm=skip_llm,
                pass_d_apply_edges=pass_d_apply_edges,
                min_chars=chunk_min_chars,
                max_chars=chunk_max_chars,
                dataset=dataset,
                pass_c_cluster_batch_size=pass_c_cluster_batch_size,
                pass_c_max_outer_rounds=pass_c_max_outer_rounds,
            )

        raw = source_path.read_text(encoding="utf-8", errors="replace")
        chunks = chunk_merged_txt(raw, min_chars=chunk_min_chars, max_chars=chunk_max_chars)
        if max_chunks and max_chunks > 0:
            chunks = chunks[: int(max_chunks)]

        if progress is not None:
            progress.chunking_done(len(chunks))

        pass_a_rows: list[dict[str, Any]] = []
        for i, ch in enumerate(chunks):
            if progress is not None:
                progress.pass_a_chunk_start(i, len(chunks), ch.source_doc)
            try:
                parsed = extract_chunk(ch, skip_llm=skip_llm, progress=progress)
            except Exception as e:
                if progress is not None:
                    progress.pass_a_chunk_error(
                        i,
                        len(chunks),
                        ch.source_doc,
                        str(e),
                        char_start=ch.char_start,
                        char_end=ch.char_end,
                        chunk_preview=ch.text,
                    )
                raise
            row = dict(parsed)
            row.pop("_ingest_from_cache", None)
            row.pop("_ingest_skip_llm", None)
            row["_source_doc"] = ch.source_doc
            pass_a_rows.append(row)
            if progress is not None:
                n_ent = len(row.get("entities") or [])
                n_ev = len(row.get("events") or [])
                try:
                    conf = float(row.get("self_confidence")) if row.get("self_confidence") is not None else None
                except (TypeError, ValueError):
                    conf = None
                from_cache = bool(parsed.get("_ingest_from_cache"))
                progress.pass_a_chunk_done(
                    i,
                    len(chunks),
                    ch.source_doc,
                    n_entities=n_ent,
                    n_events=n_ev,
                    confidence=conf,
                    from_cache=from_cache,
                    char_start=ch.char_start,
                    char_end=ch.char_end,
                    chunk_text=ch.text,
                    entities=_ui_entity_rows(row),
                    events_preview=_ui_event_rows(row),
                )

        surface_records = collect_surface_records(pass_a_rows)
        clusters = run_pass_b(surface_records, skip_llm=skip_llm, progress=progress)
        if progress is not None:
            progress.pass_b_done(len(surface_records), len(clusters))

        surface_to_canonical: dict[str, str] = {}
        for c in clusters:
            if not isinstance(c, dict):
                continue
            ck = str(c.get("canonical_key") or "").strip()
            if not ck:
                continue
            for s in list(c.get("surfaces") or []) + [str(c.get("canonical_name") or "").strip()]:
                t = str(s).strip()
                if t:
                    surface_to_canonical[t] = ck

        nc_pc = len(clusters)
        if nc_pc == 0:
            n_pass_c_llm_calls = 0
        else:
            nb_pc = (
                1
                if pass_c_cluster_batch_size <= 0
                else (nc_pc + pass_c_cluster_batch_size - 1) // pass_c_cluster_batch_size
            )
            n_pass_c_llm_calls = nb_pc * max(1, pass_c_max_outer_rounds)

        pc_out = run_pass_c_batched(
            clusters,
            batch_size=pass_c_cluster_batch_size,
            max_outer_rounds=pass_c_max_outer_rounds,
            skip_llm=skip_llm,
            progress=progress,
        )
        kg_topology = pc_out.get("kg_topology")
        if not isinstance(kg_topology, dict):
            kg_topology = {}
        n_kg_nodes = len(list(kg_topology.get("nodes") or []))
        n_kg_rels = len(list(kg_topology.get("relationships") or []))
        pass_c_conf = float(pc_out.get("self_confidence") or 0.75)
        if n_kg_nodes == 0 and len(clusters) > 0 and not skip_llm:
            log.warning(
                "pass_c produced 0 kg_topology nodes (%d clusters, batch_size=%s, outer_rounds=%s); "
                "try pass_c_cluster_batch_size=20, pass_c_max_outer_rounds=2, or clear stale "
                "%s/pass_c_*.json",
                len(clusters),
                pass_c_cluster_batch_size if pass_c_cluster_batch_size > 0 else "all",
                pass_c_max_outer_rounds,
                _CACHE_DIR_HINT,
            )
        if progress is not None:
            progress.pass_c_done(n_kg_nodes=n_kg_nodes, n_kg_rels=n_kg_rels)

        n_pass_d_edges = 0
        n_pass_d_proposed = 0
        pass_d_preview_only = False
        pass_d_llm_calls = 0
        pass_d_outer_rounds = 0

        if progress is not None:
            progress.write_begin(dry_run)

        if not dry_run:
            from neo4j import GraphDatabase

            from ..embedder import embed_one

            if work is None:
                work = GraphDatabase.driver(
                    CFG.neo4j_uri, auth=(CFG.neo4j_user, CFG.neo4j_password)
                )
                own = True
            assert work is not None
            rep = ingest_all(
                work,
                pass_a_flat=pass_a_rows,
                surface_to_canonical=surface_to_canonical,
                embedder=embed_one,
                profile=profile,
                db_id=db_id,
                schema=schema,
                dry_run=False,
                kg_topology=kg_topology,
                pass_c_confidence=pass_c_conf,
            )
        else:
            rep = IngestReport()
            rep.skipped.append(
                "dry_run: ingest_all skipped (this run's Entity/Event/kg_topology not written to Neo4j)"
            )

        if progress is not None:
            progress.pass_c_neo4j_written(
                dry_run=dry_run,
                n_kg_nodes=n_kg_nodes,
                n_kg_rels=n_kg_rels,
                n_entities=0 if dry_run else rep.pass_c_neo4j_entities,
                n_related_to=0 if dry_run else rep.pass_c_neo4j_related_to_rows,
                n_has_instance=0 if dry_run else rep.pass_c_neo4j_has_instance_pairs,
            )

        if not skip_llm:
            from neo4j import GraphDatabase

            from .pass_d import (
                apply_pass_d_edges,
                build_pass_d_payload,
                ent_cluster_batches,
                fetch_knowledge_graph_catalog,
                fetch_mg_catalog,
                fetch_trace_graph_catalog,
                fetch_zone_counts,
                filter_pass_d_edges_for_ent_batch,
                run_pass_d,
            )

            if work is None:
                work = GraphDatabase.driver(
                    CFG.neo4j_uri, auth=(CFG.neo4j_user, CFG.neo4j_password)
                )
                own = True
            assert work is not None

            do_apply = (not dry_run) or pass_d_apply_edges
            pass_d_preview_only = bool(dry_run and not pass_d_apply_edges)

            batches = ent_cluster_batches(clusters, batch_size=pass_d_entity_batch_size)
            if batches:
                multi_pass = len(batches) > 1
                effective_max_rounds = max(1, pass_d_max_rounds) if multi_pass else 1
                for rnd in range(effective_max_rounds):
                    cat = fetch_mg_catalog(work)
                    trace_cat = fetch_trace_graph_catalog(work)
                    kg_cat = fetch_knowledge_graph_catalog(work)
                    zone_counts = fetch_zone_counts(work)
                    round_proposed = 0
                    for bi, batch in enumerate(batches):
                        inc_meta: Optional[dict[str, Any]] = None
                        if multi_pass:
                            inc_meta = {
                                "round": rnd + 1,
                                "max_rounds": effective_max_rounds,
                                "batch_index": bi + 1,
                                "n_batches": len(batches),
                            }
                        pd_payload = build_pass_d_payload(
                            cat,
                            trace_catalog=trace_cat,
                            kg_catalog=kg_cat,
                            zone_counts=zone_counts,
                            clusters=batch,
                            incremental=inc_meta,
                        )
                        pd_out = run_pass_d(pd_payload, skip_llm=False, progress=progress)
                        pass_d_llm_calls += 1
                        edges = list(pd_out.get("edges") or [])
                        if inc_meta is not None:
                            edges = filter_pass_d_edges_for_ent_batch(edges, batch)
                        n_pass_d_proposed += len(edges)
                        round_proposed += len(edges)
                        if do_apply:
                            n_pass_d_edges += apply_pass_d_edges(work, edges, rep=rep)
                    pass_d_outer_rounds += 1
                    if round_proposed == 0:
                        break
            else:
                rep.skipped.append("pass_d skipped: no ent: clusters in Pass B output")

            if pass_d_preview_only and n_pass_d_proposed:
                rep.skipped.append(
                    f"pass_d: {n_pass_d_proposed} edges proposed in {pass_d_llm_calls} LLM call(s) "
                    "(MERGE skipped; use pass_d_apply_edges to write while dry_run)"
                )

            if progress is not None:
                progress.pass_d_done(
                    n_pass_d_edges,
                    n_proposed=n_pass_d_proposed,
                    preview_only=not do_apply,
                )
        elif progress is not None:
            progress.pass_d_skipped("skip_llm")

        if own and work is not None:
            try:
                work.close()
            except Exception:  # noqa: BLE001
                pass

        _write_report(
            REPORT_PATH,
            source=source_path,
            n_chunks=len(chunks),
            n_clusters=len(clusters),
            n_kg_nodes=n_kg_nodes,
            n_kg_rels=n_kg_rels,
            n_pass_d_edges=n_pass_d_edges,
            n_pass_d_proposed=n_pass_d_proposed,
            pass_d_preview_only=pass_d_preview_only,
            rep=rep,
            dry_run=dry_run,
            pass_d_llm_calls=pass_d_llm_calls,
            pass_d_outer_rounds=pass_d_outer_rounds,
            pass_d_entity_batch_size=pass_d_entity_batch_size,
            pass_c_llm_calls=n_pass_c_llm_calls,
            pass_c_cluster_batch_size=pass_c_cluster_batch_size,
            pass_c_max_outer_rounds=pass_c_max_outer_rounds,
        )

        pass_d_skipped: str | None = "skip_llm" if skip_llm else None

        out = {
            "chunks": len(chunks),
            "clusters": len(clusters),
            "pass_c_kg_nodes": n_kg_nodes,
            "kg_topology_rels": n_kg_rels,
            "pass_c_neo4j_entities": 0 if dry_run else rep.pass_c_neo4j_entities,
            "pass_c_neo4j_related_to_rows": 0 if dry_run else rep.pass_c_neo4j_related_to_rows,
            "pass_c_neo4j_has_instance_pairs": 0 if dry_run else rep.pass_c_neo4j_has_instance_pairs,
            "pass_c_cluster_batch_size": pass_c_cluster_batch_size,
            "pass_c_max_outer_rounds": pass_c_max_outer_rounds,
            "pass_c_llm_calls": n_pass_c_llm_calls,
            "pass_d_edges": n_pass_d_edges,
            "pass_d_proposed": n_pass_d_proposed,
            "pass_d_preview_only": pass_d_preview_only,
            "pass_d_apply_edges": pass_d_apply_edges,
            "pass_d_skipped": pass_d_skipped,
            "pass_d_entity_batch_size": pass_d_entity_batch_size,
            "pass_d_max_rounds": pass_d_max_rounds,
            "pass_d_llm_calls": pass_d_llm_calls,
            "pass_d_outer_rounds": pass_d_outer_rounds,
            "decisions": rep.count_by(),
            "report_path": str(REPORT_PATH),
            "dry_run": dry_run,
        }
        if progress is not None:
            progress.write_done(out)
        return out
    except Exception as e:
        if progress is not None:
            progress.fail(str(e))
        raise
