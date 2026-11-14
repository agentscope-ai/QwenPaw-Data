# -*- coding: utf-8 -*-
from __future__ import annotations

import time
import uuid

import shortuuid


def create_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def create_session_id() -> str:
    """Preserve the standalone session filename contract."""
    return f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:4]}"


def create_node_id() -> str:
    """Standalone DAG IDs are distinct from current service resource IDs."""
    return f"node_{shortuuid.uuid()[:8]}"


def create_graph_id() -> str:
    return f"graph_{shortuuid.uuid()[:8]}"
