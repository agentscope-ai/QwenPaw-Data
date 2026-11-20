# -*- coding: utf-8 -*-
"""One WARNING per tracing failure site; repeats fall back to DEBUG."""

from __future__ import annotations

import logging
from typing import Any

_warned: set[str] = set()


def warn_once(
    logger: logging.Logger, key: str, msg: str, *args: Any, **kwargs: Any
) -> None:
    """Log ``msg`` at WARNING the first time ``key`` is seen, DEBUG after."""
    if key in _warned:
        logger.debug(msg, *args, **kwargs)
        return
    _warned.add(key)
    logger.warning(msg, *args, **kwargs)


def reset_warn_once() -> None:
    """Test hook: forget which sites already warned."""
    _warned.clear()
