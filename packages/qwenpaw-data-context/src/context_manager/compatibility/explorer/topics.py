"""Workspace-owned topic jobs with develop's clustering and response contract."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx
from jsonschema import ValidationError, validate
from openai import APITimeoutError

from ...auth import GLOBAL_OWNER_ID
from ...model.llm import ChatMessage, LlmError
from ...model.usage import bind_usage_context, current_usage_context
from ...store import StoreError
from .access import ExplorerAccess
from .topic_algorithm import (
    _NAME_SYSTEM,
    CROSS_SIM_FLOOR,
    CROSS_TOP_K,
    INTRA_SIM_THRESHOLD,
    _cluster,
    _merge_duplicates,
    _topics_view,
)

log = logging.getLogger(__name__)
_LAYER_OF_LABEL = {
    **dict.fromkeys(("Metric", "Dimension", "Table", "Domain", "Dataset"), "MG"),
    **dict.fromkeys(("Entity", "Event"), "KG"),
    "UserMemory": "UG",
}
_CANDIDATE_LABELS = tuple(_LAYER_OF_LABEL)


def _fetch_candidates(access: ExplorerAccess) -> list[dict[str, Any]]:
    """Read topic candidates through the same scoped primitives as Explorer."""
    out = {}
    for node in access.nodes(_CANDIDATE_LABELS):
        props = node.properties
        label = node.spec.name
        row = {
            **props,
            "name": props.get("name") or props.get("canonical_name") or "",
        }
        layer = _LAYER_OF_LABEL[label]
        text = _node_text(row, layer, label)
        if not text and not props.get("embedding"):
            continue
        existing = out.get(node.key)
        if existing:
            continue
        out[node.key] = {
            "key": node.key,
            "layer": layer,
            "type": label.lower(),
            "label": row["name"] or node.key,
            "domain": props.get("domain") or "",
            "text": text,
            "vec": list(props["embedding"]) if props.get("embedding") else None,
            "owner": props.get("owner_id") or "",
        }
    return list(out.values())


def _node_text(row: dict[str, Any], layer: str, label: str) -> str:
    """Compose one embeddable sentence per node; empty ⇒ skipped (unless the
    node already carries a stored embedding)."""
    name = (row.get("name") or "").strip()
    if label == "UserMemory":
        desc = (row.get("description") or "").strip()
        return f"Memory: {desc}" if desc else ""
    # MG / KG: name + definition/description
    parts = [
        p
        for p in (name, row.get("definition"), row.get("description"))
        if p and p.strip()
    ]
    if not parts:
        return ""
    return f"{label}: {parts[0]}" + (f" — {parts[1]}" if len(parts) > 1 else "")


# Only relationships between topic candidate types contribute to clustering.
_HARD_EDGE_REL_TYPES = [
    "HAS",
    "SURFACE_METRIC",
    "SURFACE_DIMENSION",
]


def _fetch_hard_edges(
    access: ExplorerAccess, keys: set[str]
) -> list[tuple[str, str, str]]:
    """Read structural relations between the chosen topic candidates."""
    return list(
        dict.fromkeys(
            (edge.source_key, edge.target_key, edge.spec.relationship)
            for edge in access.edges(_HARD_EDGE_REL_TYPES, labels=_CANDIDATE_LABELS)
            if edge.source_key in keys and edge.target_key in keys
        )
    )


def _fetch_user_attachments(access: ExplorerAccess) -> dict[str, list[dict[str, str]]]:
    """Map memories to their authorized owning users."""
    result = {}
    for edge in access.edges(("HAS",), labels=("User", "UserMemory")):
        user = access.node(edge.source_key, ("User",))
        if user is not None:
            result.setdefault(edge.target_key, []).append(
                {
                    "key": user.key,
                    "label": user.properties.get("name")
                    or user.properties.get("id")
                    or user.key,
                }
            )
    return result


def candidates(service, scope):
    access = ExplorerAccess(service.store, scope)
    nodes = _fetch_candidates(access)
    edges = _fetch_hard_edges(access, {n["key"] for n in nodes})
    users = _fetch_user_attachments(access)
    return nodes, edges, users


async def localize_topic(llm, members):
    per_layer = {}
    for member in members:
        names = per_layer.setdefault(member["layer"], [])
        if member["label"] not in names:
            names.append(member["label"])
    lines = "\n".join(
        f"- {layer}: " + ", ".join(names[:12]) for layer, names in per_layer.items()
    )
    fallback = " / ".join(list(dict.fromkeys(m["label"] for m in members))[:3])

    async def name(lang):
        try:
            schema = {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["title", "summary"],
            }
            instruction = (
                "The response must match this supplied JSON Schema exactly:\n"
                + json.dumps(
                    schema, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
            )
            messages = [
                ChatMessage("system", _NAME_SYSTEM[lang] + "\n\n" + instruction),
                ChatMessage(
                    "user",
                    ("Topic members:\n" if lang == "en" else "主题成员：\n") + lines,
                ),
            ]
            # Match develop's complete_json: repair string lists, validate,
            # and retry twice before falling back to member names.
            for attempt in range(3):
                try:
                    raw = await llm.complete(
                        messages,
                        json_mode=True,
                        temperature=0.0,
                        enable_reasoning=False,
                    )
                    value = json.loads(raw or "{}")
                    if not isinstance(value, dict):
                        raise ValueError("Topic naming response must be a JSON object")
                    for field in ("title", "summary"):
                        content = value.get(field)
                        if (
                            isinstance(content, list)
                            and content
                            and all(isinstance(part, str) for part in content)
                        ):
                            parts = [part.strip() for part in content if part.strip()]
                            if parts:
                                value[field] = "\n\n".join(parts)
                    validate(value, schema)
                    return {
                        "title": value["title"].strip() or fallback,
                        "summary": value["summary"].strip(),
                    }
                except (
                    ValueError,
                    KeyError,
                    ValidationError,
                    APITimeoutError,
                    httpx.TimeoutException,
                ):
                    log.warning("Topic naming attempt %d/3 failed", attempt + 1)
                except LlmError as exc:
                    # The client wraps timeouts and empty/invalid text. Keep
                    # develop's retries for those, not other provider failures.
                    if not isinstance(
                        exc.__cause__,
                        (
                            APITimeoutError,
                            httpx.TimeoutException,
                            TypeError,
                            ValueError,
                        ),
                    ):
                        raise
                    log.warning("Topic naming attempt %d/3 failed", attempt + 1)
        except Exception:
            log.warning("Topic naming failed; using member names")
        return {"title": fallback, "summary": ""}

    zh, en = await name("zh"), await name("en")
    return zh["title"], zh["summary"], {"zh": zh, "en": en}


class TopicJobs:
    """Retain per-viewpoint results and in-flight computations within a workspace."""

    def __init__(self, service):
        self.service = service
        self.cache = {}
        self.tasks = {}

    @staticmethod
    def key(scope):
        return (
            scope.tenant_id,
            scope.workspace_id,
            scope.owner_ids,
            scope.zone,
            scope.datasource_id,
        )

    def status(self, scope, lang=None):
        key = self.key(scope)
        payload = self.cache.get(key)
        if payload is not None:
            if payload["status"] == "forbidden":
                raise PermissionError("graph read is forbidden")
            return _topics_view(payload, lang)
        return {"status": "computing" if key in self.tasks else "missing", "topics": []}

    async def compute(self, scope, models, *, recompute=False, wait=True, lang=None):
        key = self.key(scope)
        task = self.tasks.get(key)
        if task is None:
            if not recompute and self.cache.get(key, {}).get("status") == "ready":
                return self.status(scope, lang)
            self.cache.pop(key, None)
            llm_config, embed_config = await self.service.executor.run(
                models.resolve_pair
            )
            parent = current_usage_context()

            async def run():
                with bind_usage_context(
                    parent,
                    chat_id=parent.chat_id if parent.session_id else "",
                    source=parent.source if parent.session_id else "话题发现",
                    source_step=parent.source_step
                    if parent.session_id
                    else f"topic_synthesis#{key}",
                    probe=False,
                ):
                    try:
                        async with (
                            models.llm(llm_config) as llm,
                            models.embedder(embed_config) as embedder,
                        ):
                            self.cache[key] = await compute_topics(
                                self.service, scope, llm, embedder
                            )
                    except PermissionError:
                        self.cache[key] = {"status": "forbidden", "topics": []}
                    except Exception as exc:
                        if isinstance(exc, StoreError) and exc.code in {
                            "user_space_uninitialized",
                            "user_space_initializing",
                        }:
                            self.cache[key] = {
                                "status": exc.code.removeprefix("user_space_"),
                                "topics": [],
                            }
                        else:
                            log.exception("Topic computation failed")
                            self.cache[key] = {
                                "status": "error",
                                "error": str(exc),
                                "topics": [],
                            }
                    finally:
                        self.tasks.pop(key, None)

            task = self.tasks[key] = asyncio.create_task(run())
        if wait:
            await asyncio.wait({task}, timeout=120)
        return self.status(scope, lang)

    async def aclose(self):
        tasks = tuple(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def compute_topics(service, scope, llm, embedder):
    """Run develop clustering on the selected canonical graph and fixed clients."""
    t0 = time.perf_counter()
    nodes, hard_edges, users_of_memory = await service.executor.run(
        candidates, service, scope
    )
    workspace_id = scope.workspace_id
    owner_id = scope.owner_ids[0]
    operation = "read_global_mg" if owner_id == GLOBAL_OWNER_ID else "read_own_graph"
    # Collapse same-name repeats before embedding; remap hard edges onto the
    # representatives so structure survives the merge.
    reps, alias_of = _merge_duplicates(nodes)
    canon: dict[str, str] = {}
    for rep_key, aliases in alias_of.items():
        for a in aliases:
            canon[a["key"]] = rep_key
    layer_of_key = {n["key"]: n["layer"] for n in nodes}
    remapped: list[tuple[str, str, str]] = []
    seen_hard: set[tuple[str, str, str]] = set()
    cross_hard_edges = 0
    for s, t, rel in hard_edges:
        cs, ct = canon.get(s, s), canon.get(t, t)
        if cs == ct:
            continue
        item = (cs, ct, rel)
        if item in seen_hard:
            continue
        seen_hard.add(item)
        remapped.append(item)
        if layer_of_key.get(s) != layer_of_key.get(t):
            cross_hard_edges += 1
    hard_edges = remapped

    missing = [n for n in reps if n["vec"] is None and n["text"]]
    if missing:
        vectors = await embedder.embed([n["text"] for n in missing])
        for node, vector in zip(missing, vectors, strict=True):
            node["vec"] = list(vector)
    embeddable = [n for n in reps if n["vec"] is not None]
    communities, sim_edges, cluster_stats = _cluster(embeddable, hard_edges)

    # Hard edges keyed by endpoint pair for topic-internal extraction.
    hard_by_pair: dict[tuple[str, str], str] = {}
    for s, t, rel in hard_edges:
        hard_by_pair[(s, t)] = rel
        hard_by_pair[(t, s)] = rel
    # Semantic edges keyed by node index pair.
    sim_by_pair: dict[tuple[int, int], float] = {}
    for i, j, s in sim_edges:
        sim_by_pair[(i, j)] = s
        sim_by_pair[(j, i)] = s

    topics: list[dict[str, Any]] = []
    for ti, comm in enumerate(communities):
        idx_list = comm["idx"]
        members = []
        for idx in idx_list:
            n = embeddable[idx]
            # The representative plus its collapsed same-name aliases.
            for node in (n, *alias_of.get(n["key"], ())):
                members.append(
                    {
                        "key": node["key"],
                        "layer": node["layer"],
                        "type": node["type"],
                        "label": node["label"],
                        "domain": node["domain"],
                        "text": (node.get("text") or "")[:240],
                    }
                )
                # Attach the owning user(s) of a memory as UG facets.
                if node["layer"] == "UG":
                    for u in users_of_memory.get(node["key"], []):
                        members.append(
                            {
                                "key": u["key"],
                                "layer": "UG",
                                "type": "user",
                                "label": u["label"],
                                "domain": "",
                                "text": "",
                            }
                        )
        # Topic-internal edges: real graph relations + semantic affinity.
        edges: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for a in range(len(idx_list)):
            for b in range(a + 1, len(idx_list)):
                ka, kb = embeddable[idx_list[a]]["key"], embeddable[idx_list[b]]["key"]
                if ka == kb:
                    continue
                pair = (ka, kb) if ka < kb else (kb, ka)
                if pair in seen:
                    continue
                rel = hard_by_pair.get((ka, kb))
                if rel:
                    edges.append({"s": ka, "t": kb, "rel": rel, "kind": "graph"})
                    seen.add(pair)
                sim = sim_by_pair.get((idx_list[a], idx_list[b]))
                if sim is not None:
                    edges.append(
                        {
                            "s": ka,
                            "t": kb,
                            "rel": "",
                            "kind": "semantic",
                            "sim": round(sim, 3),
                        }
                    )
                    seen.add(pair)
        title, summary, named = await localize_topic(llm, members)
        layer_counts: dict[str, int] = {}
        for m in members:
            layer_counts[m["layer"]] = layer_counts.get(m["layer"], 0) + 1
        topics.append(
            {
                "id": f"t{ti}",
                "title": title,
                "summary": summary,
                "i18n": named,
                "members": members,
                "edges": edges,
                "layer_counts": layer_counts,
                "size": len(members),
                "cross_layer": comm["cross_layer"],
            }
        )

    # Structured emptiness/degradation diagnosis — the UI turns the code into
    # an actionable message instead of a bare "no topics".
    candidate_layers: dict[str, int] = {}
    for n in nodes:
        candidate_layers[n["layer"]] = candidate_layers.get(n["layer"], 0) + 1
    if any(t["cross_layer"] for t in topics):
        reason = "ok"
    elif operation == "read_global_mg":
        # Admin global viewpoint is global-MG-only by design (KG/UG are
        # user-owned layers) — cross-layer topics cannot exist here.
        reason = "global_mg_scope"
    elif len(candidate_layers) < 2:
        reason = "single_layer_candidates"
    elif cluster_stats["cross_semantic_edges"] + cross_hard_edges == 0:
        reason = "no_cross_links"
    else:
        reason = "cross_links_too_weak"

    payload = {
        "topics": topics,
        "status": "ready",
        "computed_at": int(time.time() * 1000),
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "node_count": len(nodes),
        "embeddable_count": len(embeddable),
        "merged_count": len(nodes) - len(reps),
        "sim_threshold": INTRA_SIM_THRESHOLD,  # legacy field name
        "thresholds": {
            "intra": INTRA_SIM_THRESHOLD,
            "cross_floor": CROSS_SIM_FLOOR,
            "cross_top_k": CROSS_TOP_K,
        },
        "scope_operation": operation,
        "diagnostics": {
            "layer_counts": candidate_layers,
            "cross_semantic_edges": cluster_stats["cross_semantic_edges"],
            "cross_hard_edges": cross_hard_edges,
            "max_cross_sim": cluster_stats["max_cross_sim"],
            "reason_code": reason,
        },
        "workspace_id": workspace_id,
    }
    log.info(
        "topics computed: ws=%s owner=%s op=%s nodes=%d (reps=%d) topics=%d (cross=%d) reason=%s in %dms",
        workspace_id or "(default)",
        owner_id or "-",
        operation,
        len(nodes),
        len(reps),
        len(topics),
        sum(1 for t in topics if t["cross_layer"]),
        reason,
        payload["elapsed_ms"],
    )
    return payload
