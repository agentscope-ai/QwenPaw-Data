# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from qwenpaw_data.host.core.api.models.chat import AskUserQuestionAnswerSchema
from qwenpaw_data.host.core.api.models.common import ApiModel
from qwenpaw_data.host.core.api.models.artifact import ArtifactCommentSchema
from .runtime_models import RuntimeModels


class CapabilityBridgeSchema(ApiModel):
    """Short-lived Host callback bound to one App/task identity."""

    protocol_version: Literal[1] = 1
    endpoint: str = Field(min_length=1, max_length=2048)
    token: str = Field(min_length=32, max_length=8192)

    @field_validator("endpoint")
    @classmethod
    def valid_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("endpoint must be an absolute HTTP(S) URL")
        return value.rstrip("/")


class SubmitRunRequest(ApiModel):
    runtime_models: RuntimeModels | None = Field(default=None, exclude=True, repr=False)
    protocol_version: Literal[1] = 1
    submission_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    )
    text: str = Field(min_length=1)
    datasource_id: str | None = Field(default=None, min_length=1)
    dataset_names: list[str] = Field(default_factory=list, max_length=64)
    agent_id: str = "default"
    capability_bridge: CapabilityBridgeSchema | None = None
    session_id: str | None = Field(default=None, min_length=1, max_length=256)
    attachment_ids: list[str] = Field(default_factory=list, max_length=32)
    artifact_comments: list[ArtifactCommentSchema] = Field(
        default_factory=list, max_length=64
    )

    @field_validator("text", "datasource_id")
    @classmethod
    def nonblank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("dataset_names")
    @classmethod
    def valid_dataset_names(cls, names: list[str]) -> list[str]:
        if any(not name.strip() or len(name) > 256 for name in names):
            raise ValueError(
                "dataset names must be nonblank and at most 256 characters"
            )
        return list(dict.fromkeys(name.strip() for name in names))

    @model_validator(mode="after")
    def require_datasource_or_session(self) -> SubmitRunRequest:
        if self.datasource_id is None and self.session_id is None:
            raise ValueError("datasource_id is required for a new session")
        return self

    def request_digest(self) -> str:
        payload = self.model_dump(exclude={"submission_id"})
        # Preserve protocol-1 digests for accepted submissions from older Hosts.
        for key in (
            "session_id",
            "attachment_ids",
            "artifact_comments",
            "dataset_names",
        ):
            if not payload[key]:
                payload.pop(key)
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class SubmissionRunSchema(ApiModel):
    session_id: str
    run_id: str
    status: Literal[
        "running",
        "reconciling",
        "succeeded",
        "failed",
        "cancelled",
        "interrupted",
        "unknown",
    ]
    last_sequence_number: int | None = None
    error: dict[str, Any] | None = None
    reason: str | None = None


class SubmissionLookupSchema(ApiModel):
    protocol_version: Literal[1] = 1
    submission_id: str
    state: Literal["accepted", "not_found"]
    run: SubmissionRunSchema | None = None


class SubmissionCommandRequest(ApiModel):
    protocol_version: Literal[1] = 1
    command_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    kind: Literal["answer", "cancel"]
    request_id: str | None = Field(default=None, min_length=1, max_length=256)
    answers: list[AskUserQuestionAnswerSchema] | None = None
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_payload(self) -> SubmissionCommandRequest:
        if self.kind == "answer":
            if not self.request_id or not self.answers or self.reason is not None:
                raise ValueError("answer requires request_id and answers")
        elif self.request_id is not None or self.answers is not None:
            raise ValueError("cancel accepts only an optional reason")
        return self

    def request_digest(self) -> str:
        encoded = json.dumps(
            self.model_dump(exclude={"command_id"}),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class SubmissionCommandSchema(ApiModel):
    protocol_version: Literal[1] = 1
    submission_id: str
    command_id: str
    kind: Literal["answer", "cancel"] | None = None
    state: Literal["accepted", "rejected", "unknown", "not_found"]
    reason: str | None = None
