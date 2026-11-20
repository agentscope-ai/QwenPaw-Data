"""A child-local handoff. Artifact paths are references, not grants to read files."""

from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from agentscope.message import TextBlock
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from agentscope.tool import ToolBase, ToolChunk
from pydantic import BaseModel, ConfigDict, Field, field_validator


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1)
    name: str | None = None

    @field_validator("path")
    @classmethod
    def session_relative(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            not path.parts
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in value
            or PureWindowsPath(value).drive
            or "\x00" in value
        ):
            raise ValueError("expected a session-relative artifact path")
        return path.as_posix()


class Delivery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1)
    artifacts: list[ArtifactRef] = Field(default_factory=list)


class SubmitResult(ToolBase):
    name = "submit_result"
    description = "Finish this delegation with a summary and session-relative artifact references."
    input_schema = Delivery.model_json_schema()
    is_read_only = True
    is_concurrency_safe = False

    def __init__(self) -> None:
        super().__init__()
        self.delivery: Delivery | None = None

    async def check_permissions(
        self, tool_input: dict[str, Any], context: PermissionContext
    ) -> PermissionDecision:
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW, message="Return a result to the parent"
        )

    async def call(self, **kwargs: Any) -> ToolChunk:
        if self.delivery is not None:
            raise ValueError("a result was already submitted")
        self.delivery = Delivery.model_validate(kwargs)
        return ToolChunk(content=[TextBlock(text="submitted")])
