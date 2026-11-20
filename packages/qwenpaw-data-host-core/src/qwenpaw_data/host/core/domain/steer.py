# -*- coding: utf-8 -*-
from __future__ import annotations


class SteerError(Exception):
    """Base error for Steer operations."""


class SteerChatEndedError(SteerError):
    """Chat ended before the steer could be accepted."""

    def __init__(self, message: str = "active chat has ended") -> None:
        super().__init__(message)
