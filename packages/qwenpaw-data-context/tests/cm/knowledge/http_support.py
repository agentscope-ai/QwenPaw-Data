"""Verify the authenticated multipart Knowledge adapter stays thin."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

from context_manager.runtime import ContextRuntime
from context_manager.schema import GRAPH_SCHEMA
from context_manager.sql import RdbConnector
from docx import Document

from cm.infrastructure.access.support import authorization
from cm.infrastructure.lifecycle.support import bound_context_runtime
from cm.infrastructure.models.support import StaticModelService
from cm.infrastructure.storage.schema_support import make_glossary
from cm.knowledge.document_support import (
    wait_for_imports as wait_for_terminal_state,
)
from cm.knowledge.support import MemoryGraphStore


class ApiKnowledgeCaller:
    """Return one deterministic entity-only extraction for the HTTP route."""

    def __init__(self) -> None:
        """Initialize lifecycle counters."""
        self.close_calls = 0

    async def complete(self, messages: object, **options: object) -> str:
        """Return one Entity terms term."""
        if "same real-world Entity or Event" in messages[0].content:
            request = json.loads(messages[1].content)
            return json.dumps(
                {
                    "decision": "match",
                    "candidate_ids": [request["candidates"][0]["candidate_id"]],
                    "confidence": 0.95,
                }
            )
        return json.dumps(
            {
                "entities": [
                    {
                        "ref": "e1",
                        "properties": {
                            "name": "AcmeChat",
                            "canonical_name": "AcmeChat",
                            "type": "product",
                            "aliases": [],
                            "description": None,
                            "lifecycle_state": "active",
                        },
                        "confidence": 0.95,
                    }
                ],
                "events": [],
                "relationships": [],
            }
        )

    async def embed(
        self, texts: object, **options: object
    ) -> tuple[tuple[float, ...], ...]:
        """Return one embedding per input."""
        return tuple((1.0, 0.0) for _ in texts)

    async def aclose(self) -> None:
        """Count application-lifetime closure."""
        self.close_calls += 1


def _content() -> bytes:
    """Create one small DOCX upload body."""
    document = Document()
    document.add_paragraph("AcmeChat is a product.")
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _runtime(
    qwenpaw_data_home: Path, *, configured: bool = True
) -> tuple[ContextRuntime, ApiKnowledgeCaller]:
    """Build one request-local runtime without physical services."""
    store = MemoryGraphStore()
    glossary = make_glossary(GRAPH_SCHEMA)
    caller = ApiKnowledgeCaller()
    runtime = bound_context_runtime(
        store,
        RdbConnector(),
        glossary,
        models=(StaticModelService(caller) if configured else None),
        qwenpaw_data_home=qwenpaw_data_home,
    )
    return runtime, caller


def wait_for_imports(client, runtime, user="user-1"):
    context = authorization(user)
    client.portal.call(wait_for_terminal_state, runtime.knowledge_importer(context))
