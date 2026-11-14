"""Strict v1 UI contracts. This protocol never authorizes business writes."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

Nonempty = Annotated[str, Field(min_length=1, pattern=r"\S")]
PositiveId = Annotated[int, Field(strict=True, gt=0, le=9007199254740991)]
PageId = Literal[
    "domains",
    "datasets",
    "columns",
    "dimensions",
    "dimension_values",
    "dimension_calibers",
    "metrics",
    "metric_formulas",
    "data_sources",
    "connections",
    "weaving",
    "profiling",
    "profiling_detail",
    "kg_docs",
    "graph_dashboard",
    "graph_global",
    "graph_management",
    "graph_explore",
    "model_settings",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DatasetRef(StrictModel):
    kind: Literal["cm.dataset"]
    datasource_id: Nonempty
    domain_id: PositiveId
    dataset_id: PositiveId


class ArtifactRef(StrictModel):
    kind: Literal["artifact"]
    session_id: Nonempty
    artifact_id: Nonempty


ObjectRef = Annotated[DatasetRef | ArtifactRef, Field(discriminator="kind")]


class InspectArgs(StrictModel):
    scope: Literal["current"]


class OpenPageArgs(StrictModel):
    page_id: PageId
    params: dict[str, Nonempty]

    @model_validator(mode="after")
    def validate_page(self):
        if set(self.params) != (
            {"datasourceId"} if self.page_id == "profiling_detail" else set()
        ):
            raise ValueError("INVALID_ARGUMENT: page parameters")
        return self


class OpenObjectArgs(StrictModel):
    object_ref: ObjectRef


class SimpleAction(StrictModel):
    type: Literal["click", "focus", "clear"]


class FillAction(StrictModel):
    type: Literal["fill"]
    value: str


class SelectAction(StrictModel):
    type: Literal["select"]
    value: list[Nonempty]


class ScrollAction(StrictModel):
    type: Literal["scroll"]
    anchor_id: Nonempty


class StateAction(StrictModel):
    type: Literal["set_state"]
    tab_id: Nonempty | None = None
    expanded: bool | None = None
    visible: bool | None = None

    @model_validator(mode="after")
    def single_state(self):
        keys = self.model_fields_set - {"type"}
        if len(keys) != 1 or getattr(self, next(iter(keys))) is None:
            raise ValueError("INVALID_ARGUMENT: exactly one state")
        return self


Action = Annotated[
    SimpleAction | FillAction | SelectAction | ScrollAction | StateAction,
    Field(discriminator="type"),
]


class ActArgs(StrictModel):
    element_id: Nonempty
    expected_view_revision: Nonempty
    action: Action


ARGUMENT_TYPES = {
    "ui_inspect": InspectArgs,
    "ui_open_page": OpenPageArgs,
    "ui_open_object": OpenObjectArgs,
    "ui_act": ActArgs,
}
DESCRIPTIONS = {
    "ui_inspect": "Read registered elements in the current view. Inspect before each action. No business data scraping.",
    "ui_open_page": "Open a registered page; profiling_detail requires datasourceId, all others require empty params. Never saves.",
    "ui_open_object": "Open an authorized dataset or artifact by its exact reference. Resolve datasets with cm_dataset_lookup first. Never saves.",
    "ui_act": "Operate one registered element using the latest view revision. Only local unsaved drafts; no save, delete or admin actions.",
}
TOOL_DEFINITIONS = [
    {
        "name": name,
        "description": DESCRIPTIONS[name],
        "parameters": model.model_json_schema(),
    }
    for name, model in ARGUMENT_TYPES.items()
]


class UiOption(StrictModel):
    id: Nonempty
    label: str


class UiElement(StrictModel):
    element_id: Nonempty
    allowed_actions: list[
        Literal["click", "focus", "clear", "fill", "select", "scroll", "set_state"]
    ]
    object_ref: ObjectRef | None = None
    value_type: Literal["text", "single_select", "multi_select"] | None = None
    options: list[UiOption] | None = None


class UiResult(StrictModel):
    object_ref: ObjectRef | None = None
    page_id: PageId | Literal["manual_only"] | None = None
    view_revision: Nonempty
    completed_steps: list[str]
    saved: Literal[False]
    view_state: str | None = None
    visible: bool | None = None
    value: str | list[str] | None = None
    elements: list[UiElement] | None = None


class UiError(StrictModel):
    code: Nonempty
    message: str


class UiAnswerExecuted(StrictModel):
    call_id: Nonempty
    status: Literal["executed"]
    result: UiResult


class UiAnswerFailed(StrictModel):
    call_id: Nonempty
    status: Literal["failed", "rejected"]
    error: UiError


UiAnswer = Annotated[UiAnswerExecuted | UiAnswerFailed, Field(discriminator="status")]
ANSWER_ADAPTER = TypeAdapter(UiAnswer)
