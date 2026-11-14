# -*- coding: utf-8 -*-
from __future__ import annotations

import uuid


def create_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"
