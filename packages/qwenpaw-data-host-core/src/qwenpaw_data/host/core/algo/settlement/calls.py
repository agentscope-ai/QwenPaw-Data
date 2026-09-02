# -*- coding: utf-8 -*-
"""Typed structured calls for settlement, on top of the shared StructuredLLM."""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from qwenpaw_data.host.core.algo.biztrace.llm import (
    StructuredLLM,
    StructuredLLMError,
)

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class StructuredCallError(RuntimeError):
    """The model could not produce a schema-conforming result."""


async def structured_call(
    llm: StructuredLLM,
    *,
    system: str,
    user: str,
    schema: type[SchemaT],
) -> SchemaT:
    """Return a validated instance of ``schema``; raise StructuredCallError."""
    try:
        payload = await llm.complete(
            system=system,
            user=user,
            schema=schema.model_json_schema(),
            schema_name=schema.__name__,
        )
        return schema.model_validate(payload)
    except StructuredLLMError as exc:
        raise StructuredCallError(str(exc)) from exc
    except Exception as exc:
        raise StructuredCallError(
            f"{schema.__name__} validation failed: {exc}"
        ) from exc
