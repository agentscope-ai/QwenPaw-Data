from __future__ import annotations

import uuid

from qwenpaw_data.host.core.utils.ids import create_id


def test_create_id_uses_prefix_and_uuid4_hex() -> None:
    value = create_id("ses")
    prefix, raw = value.split("-", 1)
    parsed = uuid.UUID(hex=raw)

    assert prefix == "ses"
    assert "-" not in raw
    assert len(raw) == 32
    assert parsed.version == 4
    assert value == f"ses-{parsed.hex}"
