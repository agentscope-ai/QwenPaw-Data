# -*- coding: utf-8 -*-
from __future__ import annotations

import json

from qwenpaw_data.tracing.core.utils.serialize import dump_json, plain


def test_plain_walks_containers_and_model_dump() -> None:
    class _Model:
        def model_dump(self, mode="json"):
            return {"n": 1}

    class _Legacy:
        def model_dump(self):
            return {"n": 2}

    assert plain([{"k": _Model()}]) == [{"k": {"n": 1}}]
    assert plain(_Legacy()) == {"n": 2}


def test_dump_json_never_raises() -> None:
    assert json.loads(dump_json({"a": 1})) == {"a": 1}

    class _Boom:
        def model_dump(self, mode="json"):
            raise RuntimeError("no")

        def __str__(self) -> str:
            return "boom"

    assert dump_json(_Boom()) == '"boom"'
