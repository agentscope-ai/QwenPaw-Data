# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime

from qwenpaw_data.host.core.api.models.common import ApiModel


class ArtifactSchema(ApiModel):
    id: str
    session_id: str
    chat_id: str | None = None
    name: str
    path: str
    created_at: datetime
    updated_at: datetime
