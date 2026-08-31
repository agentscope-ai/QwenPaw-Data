# -*- coding: utf-8 -*-
from __future__ import annotations

import json

from qwenpaw_data.host.core.api.models.stream_objects import (
    StreamObject,
    dump_stream_object,
)


def format_sse(obj: StreamObject) -> str:
    data = json.dumps(
        dump_stream_object(obj),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"id: {obj.sequence_number}\nevent: {obj.object}\ndata: {data}\n\n"
