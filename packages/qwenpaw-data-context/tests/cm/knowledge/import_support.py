"""Deterministic knowledge-model responses and uploaded documents shared across layers."""

from __future__ import annotations

import json
from io import BytesIO

from context_manager.knowledge import UploadedDocument
from docx import Document


class FakeKnowledgeCaller:
    """Return deterministic KG JSON and embeddings without network calls."""

    def __init__(
        self,
        response: dict[str, object],
        *,
        batch_decision: str = "new",
        resolution_decision: str = "match",
    ) -> None:
        """Bind extraction and Knowledge identity responses with counters."""
        self.response = response
        self.batch_decision = batch_decision
        self.resolution_decision = resolution_decision
        self.complete_calls = 0
        self.complete_options: list[dict[str, object]] = []
        self.embed_calls = 0
        self.embed_inputs: list[tuple[str, ...]] = []
        self.close_calls = 0

    async def complete(self, messages: object, **options: object) -> str:
        """Return extraction JSON or a mode-specific semantic decision."""
        self.complete_calls += 1
        self.complete_options.append(dict(options))
        system = getattr(messages[0], "content", "")
        if "same real-world Entity or Event" in system:
            request = json.loads(messages[1].content)
            candidates = request["candidates"]
            decision = (
                self.batch_decision
                if request["mode"] == "batch_deduplication"
                else self.resolution_decision
            )
            candidate_ids = (
                []
                if decision == "new"
                else [candidate["candidate_id"] for candidate in candidates]
            )
            if decision == "match":
                candidate_ids = candidate_ids[:1]
            return json.dumps(
                {
                    "decision": decision,
                    "candidate_ids": candidate_ids,
                    "confidence": 0.95,
                }
            )
        return json.dumps(self.response, ensure_ascii=False)

    async def embed(
        self, texts: object, **options: object
    ) -> tuple[tuple[float, ...], ...]:
        """Return one stable non-zero vector for every input."""
        self.embed_calls += 1
        self.embed_inputs.append(tuple(texts))
        return tuple((1.0, 0.0, 0.0) for _ in texts)

    async def aclose(self) -> None:
        """Count caller closure."""
        self.close_calls += 1


def _docx(text: str) -> UploadedDocument:
    """Build one in-memory DOCX upload."""
    document = Document()
    document.add_paragraph(text)
    buffer = BytesIO()
    document.save(buffer)
    return UploadedDocument(
        "report.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        buffer.getvalue(),
    )


def _response(
    *,
    entity_type: str = "product",
    type_description: str | None = None,
) -> dict[str, object]:
    """Return one Entity, one Event, and one valid relationship."""
    entity: dict[str, object] = {
        "ref": "entity-1",
        "properties": {
            "name": "AcmeChat",
            "canonical_name": "AcmeChat",
            "type": entity_type,
            "aliases": ["示例助手"],
            "description": "An AI product",
            "lifecycle_state": "active",
        },
        "confidence": 0.96,
    }
    if type_description is not None:
        entity["type_description"] = type_description
    return {
        "entities": [entity],
        "events": [
            {
                "ref": "event-1",
                "properties": {
                    "name": "AcmeChat launched a new feature",
                    "type": "launch",
                    "description": "A product update was released",
                    "scope": "global",
                    "date_from": "2026-08-01T00:00:00Z",
                    "date_to": None,
                },
                "confidence": 0.91,
            }
        ],
        "relationships": [
            {
                "source_ref": "entity-1",
                "relationship": "PARTICIPATION",
                "target_ref": "event-1",
                "properties": {
                    "role": "subject",
                    "description": "AcmeChat is the subject of the update",
                },
                "confidence": 0.93,
            }
        ],
    }
