# -*- coding: utf-8 -*-
"""BizTrace public surface.

Heavy modules (pipeline / transformer) are imported lazily so
``import qwenpaw_data.host.core.algo.biztrace.models`` does not pull the
persistence layer through the confidence grader.
"""

from __future__ import annotations

from typing import Any

from qwenpaw_data.host.core.algo.biztrace.models import BizEvent, FrontendEvent, Segment
from qwenpaw_data.host.core.algo.biztrace.settings import BizTraceSettings

__all__ = [
    "BizEvent",
    "BizTracePipeline",
    "BizTraceSettings",
    "BizTraceTransformer",
    "FrontendEvent",
    "Segment",
    "build_pipeline",
]


def __getattr__(name: str) -> Any:
    if name in {"BizTracePipeline", "build_pipeline"}:
        from qwenpaw_data.host.core.algo.biztrace.pipeline import (
            BizTracePipeline,
            build_pipeline,
        )

        mapping = {
            "BizTracePipeline": BizTracePipeline,
            "build_pipeline": build_pipeline,
        }
        return mapping[name]
    if name == "BizTraceTransformer":
        from qwenpaw_data.host.core.algo.biztrace.transformer import BizTraceTransformer

        return BizTraceTransformer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
