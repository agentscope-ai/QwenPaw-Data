# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator


class TZDateTime(TypeDecorator):
    """Always expose timezone-aware UTC datetimes to Python.

    SQLAlchemy's DateTime(timezone=True) is a no-op on SQLite (no native
    timestamptz). This decorator is the documented recipe: reject naive
    values on write, normalize to UTC, and restore tzinfo on read.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            if not value.tzinfo or value.tzinfo.utcoffset(value) is None:
                raise TypeError("tzinfo is required")
            value = value.astimezone(datetime.timezone.utc)
        return value

    def process_result_value(self, value, dialect):
        if value is not None:
            if value.tzinfo is None:
                value = value.replace(tzinfo=datetime.timezone.utc)
            else:
                value = value.astimezone(datetime.timezone.utc)
        return value
