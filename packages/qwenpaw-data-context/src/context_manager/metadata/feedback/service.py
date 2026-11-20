"""Prepare settlement feedback against personal MG, then publish atomically."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

from ...auth import AccessMode, ScopeBinding
from ...executor import BoundedExecutor
from ...model import LlmError, ModelService
from ...model.usage import bind_usage_context
from ...process.embedding import EmbeddingEnricher
from ...schema import (
    AVAILABLE_IN,
    CALIBER_ON,
    DATA_SOURCE,
    DATASET,
    DATASET_FIELD,
    DATASET_HAS_COLUMN_DATASET_FIELD,
    DIMENSION,
    DIMENSION_BINDING,
    DOMAIN,
    HAS_CALIBER,
    HAS_DATASET,
    HAS_DIMENSION,
    HAS_METRIC,
    MAPS_TO_FIELD,
    METRIC,
    METRIC_CALIBER,
    MG_SYNC_STATE,
    PropertyOwner,
)
from ...store import EdgeDraft, GraphStore, NodeDraft
from ...transaction import StoreOperation
from .contracts import CardAck, CardWarning, FeedbackCard
from .screening import screen_card


class FeedbackError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code, self.status_code = code, status_code


@dataclass
class FeedbackPlan:
    """One request's prepared changes; never retained by the service."""

    scope: ScopeBinding
    nodes: dict
    edges: dict
    observations: dict
    keys: list[str]
    digest: str


class MetadataFeedbackService:
    def __init__(
        self,
        store: GraphStore,
        models: ModelService,
        *,
        executor: BoundedExecutor,
    ):
        self._store, self._models = store, models
        self._executor = executor

    def _require_scope(self, scope):
        self._store.access.require(scope, AccessMode.WRITE)
        if (
            scope.write_owner_id != scope.user_id
            or scope.zone != "metadata"
            or scope.datasource_id is not None
        ):
            raise PermissionError(
                "Feedback requires the actor's personal metadata scope"
            )

    async def ingest(
        self, card: FeedbackCard, scope: ScopeBinding, *, mode="test"
    ) -> CardAck:
        async def work():
            self._require_scope(scope)
            if mode not in {"test", "confirm"}:
                raise ValueError("mode must be test or confirm")
            plan = await self._executor.run_sync(self._prepare, card, scope)
            duplicate = self._duplicate(plan)
            screen = None
            if not duplicate:
                screen = await screen_card(
                    self._store,
                    self._models,
                    card,
                    plan,
                    scope,
                    run_sync=self._executor.run_sync,
                )
                if screen.verdict in {"CONFLICT", "ADDITIVE"}:
                    merged = await self._executor.run_sync(
                        self._prepare, card, scope, screen
                    )
                    if any(
                        merged.observations[identity] != expected
                        for identity, expected in plan.observations.items()
                        if identity in merged.observations
                    ):
                        raise FeedbackError(
                            "STALE_FEEDBACK", "筛查期间元数据已变化，请重新提交", 409
                        )
                    plan = merged
                    duplicate = self._duplicate(plan)
            status = (
                "duplicate"
                if duplicate
                else (
                    "updated"
                    if any(
                        record is not None
                        for (spec, key), record in plan.observations.items()
                        if key in plan.keys
                    )
                    else "accepted"
                )
            )
            warnings = []
            if screen is not None and screen.verdict == "SKIPPED":
                warnings.append(
                    CardWarning(code="SCREEN_SKIPPED", message=screen.reason)
                )
            if mode == "confirm" and not duplicate:
                # Models run before the transaction. Failed embedding clears stale vectors.
                drafts = tuple(draft for draft, _ in plan.nodes.values())
                try:
                    async with self._models.embedder() as embedder:
                        prepared, pending = await self._executor.run_sync(
                            EmbeddingEnricher.prepare, drafts
                        )
                        vectors = (
                            await embedder.embed(tuple(text for _, text in pending))
                            if pending
                            else ()
                        )
                        drafts = await self._executor.run_sync(
                            EmbeddingEnricher.apply, prepared, pending, vectors
                        )
                except LlmError:
                    warnings.append(
                        CardWarning(
                            code="EMBEDDING_SKIPPED",
                            message="向量生成不可用，内容已保留，可稍后重建向量",
                        )
                    )
                await self._executor.run_sync(self._execute, plan, drafts)
            return CardAck(
                client_card_id=card.client_card_id,
                type=card.type,
                status=status,
                mode=mode,
                keys=plan.keys,
                screen=screen,
                warnings=warnings,
            )

        with bind_usage_context(
            source="反馈沉淀",
            source_step=f"feedback_card#{card.client_card_id}"
            if card.client_card_id
            else "feedback_card",
            session_id="",
            chat_id="",
            probe=False,
        ):
            return await self._executor.run(work)

    @staticmethod
    def _duplicate(plan):
        primaries = [
            node for (_, key), node in plan.observations.items() if key in plan.keys
        ]
        return bool(primaries) and all(
            node is not None and node.properties.get("card_content_hash") == plan.digest
            for node in primaries
        )

    def _prepare(self, card, scope, screen=None):
        states = self._store.list_nodes(
            MG_SYNC_STATE, scope=self._store.access.internal_read_scope(scope), limit=2
        )
        if not states or not json.loads(states[0].properties["state"]).get(
            "last_synced_digest"
        ):
            raise FeedbackError(
                "SYNC_REQUIRED", "请先同步一次 Metadata Graph，再提交反馈", 409
            )
        domains = []
        for field in ("domain_id", "name"):
            domains.extend(
                self._store.list_nodes(
                    DOMAIN, filters={field: card.domain}, scope=scope, limit=2
                )
            )
        domains = {n.key: n for n in domains}
        if len(domains) != 1:
            raise FeedbackError("INVALID_DOMAIN", "业务域不存在或名称不唯一")
        domain = next(iter(domains.values()))
        domain_id = domain.properties["domain_id"]
        allowed = domain.properties.get("datasource_scope")
        if allowed and card.datasource_id not in allowed:
            raise FeedbackError("INVALID_DATASOURCE", "数据源不属于该业务域")
        ds_scope = self._store.access.narrow_write_scope(
            scope, datasource_id=card.datasource_id
        )
        sources = self._store.list_nodes(DATA_SOURCE, scope=ds_scope, limit=2)
        if len(sources) != 1:
            raise FeedbackError("INVALID_DATASOURCE", "请先同步该数据源的元数据")
        source = sources[0]
        content = card.model_dump(mode="json", exclude={"client_card_id", "extra"})
        content["domain"] = domain_id
        digest = sha256(
            json.dumps(content, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        plan = FeedbackPlan(
            scope,
            {},
            {},
            {(DOMAIN, domain.key): domain, (DATA_SOURCE, source.key): source},
            [],
            digest,
        )
        provenance = {
            "card_client_id": card.client_card_id,
            "card_content_hash": digest,
            "feedback_source": "online_feedback",
            "feedback_extra": json.dumps(
                card.extra, ensure_ascii=False, sort_keys=True
            ),
        }
        now = datetime.now(UTC)

        def node(spec, values, *, primary=False, placeholder=False):
            key = spec.property("key").generate(
                {"owner_id": scope.write_owner_id, **values}
            )
            if (
                primary
                and screen
                and screen.matched_label == spec.name
                and screen.matched_key
            ):
                key = screen.matched_key
            identity = (spec, key)
            if identity not in plan.observations:
                plan.observations[identity] = self._store.get_nodes(
                    spec, (key,), scope=scope
                )[0]
            existing = plan.observations[identity]
            datasource = card.datasource_id
            if existing:
                datasource = existing.properties.get("datasource_id")
                if datasource not in {None, card.datasource_id}:
                    raise FeedbackError(
                        "DATASOURCE_CONFLICT", "同名元数据属于其他数据源，不能覆盖", 409
                    )
                if (
                    spec is DATASET
                    and existing.properties.get("datasource_id") != card.datasource_id
                ):
                    raise FeedbackError(
                        "DATASOURCE_CONFLICT", "数据集属于其他数据源，不能覆盖", 409
                    )
                original = {
                    p.name: existing.properties[p.name]
                    for p in spec.properties
                    if p.owner is PropertyOwner.CALLER and p.name in existing.properties
                }
                if placeholder:
                    return key
                # A screened alias retains the canonical object's immutable identity.
                values = {
                    **original,
                    **{
                        k: v
                        for k, v in values.items()
                        if spec.property(k).editable and not (k == "expr" and not v)
                    },
                }
                if (
                    screen
                    and screen.verdict == "ADDITIVE"
                    and primary
                    and "description" in values
                ):
                    values["description"] = _append(
                        original.get("description"), values["description"]
                    )
            if primary:
                plan.keys.append(key)
                values.update(provenance)
            values["updated_at"] = now
            if spec.embedding_fields:
                values.update(embedding=None, embedding_hash=None)
            draft = NodeDraft.build(self._store.schema, spec, key, values)
            plan.nodes[spec, key] = (draft, datasource)
            return key

        def edge(spec, start, end, values=None):
            identity = (spec, start, end)
            draft = EdgeDraft.build(self._store.schema, spec, start, end, values or {})
            existing = self._store.get_edges(spec, ((start, end),), scope=scope)[0]
            plan.edges[identity] = (draft, existing)

        def dataset(name):
            key = node(
                DATASET,
                {
                    "domain": domain_id,
                    "name": name,
                    "source": "",
                },
                placeholder=True,
            )
            edge(HAS_DATASET, domain.key, key)
            edge(AVAILABLE_IN, key, source.key)
            return key

        kind = card.type.value
        if kind == "metric_caliber":
            ds_key = dataset(card.dataset)
            values = {
                "domain": domain_id,
                "name": card.name,
                "description": card.description,
            }
            if card.unit:
                values["unit"] = card.unit
            metric = node(METRIC, values, primary=True)
            metric_name = plan.nodes[METRIC, metric][0].properties["name"]
            caliber = node(
                METRIC_CALIBER,
                {
                    "domain": domain_id,
                    "metric_name": metric_name,
                    "dataset": card.dataset,
                    "time_window": "",
                    "expr": card.formula,
                    "source": "online_feedback",
                },
            )
            edge(HAS_METRIC, domain.key, metric)
            edge(HAS_CALIBER, metric, caliber)
            edge(CALIBER_ON, caliber, ds_key)
        elif kind == "dimension_def":
            ds_key = dataset(card.dataset_name)
            field = node(
                DATASET_FIELD,
                {
                    "domain": domain_id,
                    "dataset": card.dataset_name,
                    "name": card.maps_to_column,
                },
                placeholder=True,
            )
            dim = node(
                DIMENSION, {"domain": domain_id, "name": card.name}, primary=True
            )
            edge(HAS_DIMENSION, domain.key, dim)
            edge(DATASET_HAS_COLUMN_DATASET_FIELD, ds_key, field)
            binding = {
                "expr": card.maps_to_column,
                "value_example": card.values,
                "source": "online_feedback",
                "updated_at": now,
            }
            edge(MAPS_TO_FIELD, dim, field, binding)
            edge(DIMENSION_BINDING, dim, ds_key, binding)
        elif kind == "column_meaning":
            ds_key = dataset(card.dataset_name)
            field = node(
                DATASET_FIELD,
                {
                    "domain": domain_id,
                    "dataset": card.dataset_name,
                    "name": card.column,
                    "description": card.description,
                },
                primary=True,
            )
            edge(DATASET_HAS_COLUMN_DATASET_FIELD, ds_key, field)
        else:
            for name in dict.fromkeys(card.datasets):
                key = dataset(name)
                existing = plan.observations[DATASET, key]
                values = {
                    "domain": domain_id,
                    "name": name,
                    "source": "",
                    "description": _append(
                        existing.properties.get("description") if existing else None,
                        card.description,
                    ),
                }
                if existing:
                    values["source"] = existing.properties["source"]
                node(DATASET, values, primary=True)
        return plan

    def _operations(self, plan):
        operations = []
        for (spec, key), (draft, datasource) in plan.nodes.items():
            existing = plan.observations[spec, key]
            if existing is None:
                operation = StoreOperation(
                    "create_nodes", {"spec": spec, "properties": (draft.output(),)}
                )
            else:
                patch = {
                    k: v
                    for k, v in draft.output().items()
                    if spec.property(k).editable and existing.properties.get(k) != v
                }
                if not patch:
                    continue
                operation = StoreOperation(
                    "patch_nodes", {"spec": spec, "patches": ((key, patch),)}
                )
            operations.append((datasource, operation))
        for (spec, start, end), (draft, existing) in plan.edges.items():
            if existing is None:
                operations.append(
                    (
                        None,
                        StoreOperation(
                            "create_edges",
                            {
                                "fields": (),
                                "spec": spec,
                                "edges": ((start, end, draft.output()),),
                            },
                        ),
                    )
                )
            else:
                patch = {
                    k: v
                    for k, v in draft.output().items()
                    if spec.property(k).editable and existing.properties.get(k) != v
                }
                if patch:
                    operations.append(
                        (
                            existing.properties.get("datasource_id"),
                            StoreOperation(
                                "patch_edges",
                                {"spec": spec, "patches": ((start, end, patch),)},
                            ),
                        )
                    )
        return tuple(operations)

    def _execute(self, plan, drafts):
        for identity, draft in zip(plan.nodes, drafts, strict=True):
            plan.nodes[identity] = (draft, plan.nodes[identity][1])
        operations = self._operations(plan)

        def publish(store):
            for (spec, key), expected in plan.observations.items():
                if store.get_nodes(spec, (key,), scope=plan.scope)[0] != expected:
                    raise FeedbackError(
                        "STALE_FEEDBACK", "元数据已变化，请重新预检并提交", 409
                    )
            for (spec, start, end), (_, expected) in plan.edges.items():
                if (
                    store.get_edges(spec, ((start, end),), scope=plan.scope)[0]
                    != expected
                ):
                    raise FeedbackError(
                        "STALE_FEEDBACK", "元数据关系已变化，请重新预检并提交", 409
                    )
            for datasource, operation in operations:
                scope = store.access.narrow_write_scope(
                    plan.scope, datasource_id=datasource
                )
                # The observations above establish the targets before command writes.
                getattr(store, operation.name)(**operation.parameters, scope=scope)

        self._store.atomic(publish)


def _append(existing, incoming):
    parts = [part for part in (existing or "").split("\n") if part]
    if incoming and incoming not in parts:
        parts.append(incoming)
    return "\n".join(parts)
