# -*- coding: utf-8 -*-
from __future__ import annotations

import time
import uuid

import shortuuid


def create_id(prefix: str, *, length: int = 8) -> str:
    return f"{prefix}_{shortuuid.uuid()[:length]}"


def create_session_id() -> str:
    return f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:4]}"


def create_node_id() -> str:
    return create_id("node")


def create_graph_id() -> str:
    return create_id("graph")
