# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest

from qwenpaw_data.host.core.utils.safe_name import require_safe_name


def test_require_safe_name_accepts_plain_name() -> None:
    assert require_safe_name("agent-a") == "agent-a"


@pytest.mark.parametrize(
    "invalid_value",
    ["", " ", ".", "..", "a/b", "a\\b", "a\x00b", " padded"],
)
def test_require_safe_name_rejects_unsafe_values(invalid_value: str) -> None:
    with pytest.raises(ValueError):
        require_safe_name(invalid_value)
