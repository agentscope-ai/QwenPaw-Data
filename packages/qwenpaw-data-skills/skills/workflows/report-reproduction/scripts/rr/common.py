# -*- coding: utf-8 -*-
"""Shared limits, path rules and IO. No report semantics."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import sys
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, NoReturn

from pydantic import BaseModel

# --------------------------------------------------------------------------- #
# Layout and limits
# --------------------------------------------------------------------------- #

PRODUCT_DIR = "report-reproduction"
MANIFEST_FILE = "report_manifest.json"

#: Bumped for the ``validation.json`` -> ``comparison.json`` rename. Older
#: manifests are migrated in place on first load (see ``ReportStore``).
MANIFEST_SCHEMA_VERSION = 3
RUN_MANIFEST_SCHEMA_VERSION = 1

#: Reference directories this CLI knows how to execute. A newer reference is
#: refused rather than interpreted.
CONTRACT_VERSION = 1

COMPARISON_FILE = "comparison.json"
LEGACY_VALIDATION_FILE = "validation.json"

EXIT_OK = 0
EXIT_ERROR = 1

DEFAULT_TOLERANCE = 0.001

# ``--chat-id`` ends up as a file name under ``turns/``, so it is validated
# instead of sanitised: a value that would have to be rewritten is rejected,
# because a rewritten name silently splits one chat into two turn scopes.
CHAT_ID_MAX_LENGTH = 128
_CHAT_ID_RE = re.compile(r"[A-Za-z0-9_-]+")
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")

_RUN_PERIOD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_UNRESOLVED_PARAMETER_RE = re.compile(
    r"\$\{[A-Za-z_][A-Za-z0-9_]*\}|\{\{\s*[A-Za-z_][A-Za-z0-9_]*\s*\}\}"
)
_WEEK_SLOT_PARAMETER_RE = re.compile(r"^w(\d+)_(start|end)$", re.IGNORECASE)
_MONTH_SLOT_PARAMETER_RE = re.compile(r"^(.+)_(\d+)$", re.IGNORECASE)

_SOURCE_SUFFIXES = frozenset({".docx", ".html", ".htm", ".md", ".txt"})
_MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
_CONTENT_SUFFIXES = {
    "text/html": ".html",
    "text/markdown": ".md",
    "text/plain": ".txt",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}

_SYNTHETIC_SECTION_TITLES = {"正文"}

#: Where a baseline Section's numbers came from. Only artifacts landed on disk can
#: prove ``重算``; without them the numbers can only have been lifted from the
#: original report, which a reproduction is never allowed to do.
PROVENANCE_RECOMPUTED = "重算"
PROVENANCE_REFERENCE = "原报告"

#: A draft that discloses an unclosable gap says outright its numbers were not
#: independently recomputed, so the gate treats the tag as a provenance signal.
IRREPRODUCIBLE_TAG = "【无法复现】"

#: A ``queries/*.sql`` file only counts as evidence when it actually queries
#: something; an empty or comment-only stub proves no recomputation happened.
_SQL_STATEMENT_RE = re.compile(r"\bselect\b", re.IGNORECASE)

_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
LOGGER_NAME = "report-reproduction"


class ReportCliError(Exception):
    """An expected failure: reported as JSON on stdout, never as a traceback."""


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #


def setup_logging(level_name: str = "info") -> logging.Logger:
    """Send every log record to stderr so stdout stays machine-readable.

    The handler is attached to this skill's own logger rather than the root
    logger: a caller that imports this module keeps its own logging
    configuration, and a second invocation rebinds to the current
    ``sys.stderr`` instead of a stream captured by the first one.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, level_name.upper(), logging.INFO))
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    logger.addHandler(handler)
    return logger


def emit(payload: dict[str, Any]) -> None:
    """Write the one structured result of this invocation to stdout."""
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
    sys.stdout.flush()


def fail(logger: logging.Logger, message: str) -> NoReturn:
    """Report a failure to both channels and stop with ``EXIT_ERROR``."""
    logger.error("%s", message)
    emit({"ok": False, "error": message})
    raise SystemExit(EXIT_ERROR)


def write_json_atomic(path: Path, model: BaseModel) -> None:
    """Write a pydantic model atomically so no reader sees a half-written file."""
    write_text_atomic(path, model.model_dump_json(indent=2) + "\n")


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as stream:
        stream.write(text)
        temporary = Path(stream.name)
    temporary.replace(path)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_text_file(path: Path, *, label: str) -> str:
    if not path.exists():
        raise ReportCliError(f"{label}不存在：{path}")
    if path.is_dir():
        raise ReportCliError(f"{label}是目录而不是文件：{path}")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ReportCliError(f"{label}不是 utf-8 文本：{path}") from exc


def _file_has_content(path: Path) -> bool:
    """True when a landed file carries more than whitespace.

    Binary payloads count by size, text payloads must hold a visible character,
    so an empty placeholder cannot stand in for real query evidence.
    """
    try:
        if path.stat().st_size == 0:
            return False
    except OSError:
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # Binary data (xlsx, parquet, png …) is a legitimate query result.
        return True
    except OSError:
        return False
    return bool(text.strip())


def _normalized_digest(text: str) -> str:
    """Whitespace-insensitive digest, so a reformatted copy still matches."""
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()


def validated_section_id(section_id: str) -> str:
    """Reject an identifier that could not be a directory name.

    Every Section-scoped subcommand claims its turn scope first, so this check
    has to run before anything is persisted: binding a chat to an identifier
    the store is about to refuse would lock that chat out of the whole
    workflow with no way to unbind it.
    """
    normalized = (section_id or "").strip()
    if (
        not normalized
        or normalized in {".", ".."}
        or Path(normalized).name != normalized
    ):
        raise ReportCliError(f"非法的 section_id：{section_id!r}")
    return normalized


# --------------------------------------------------------------------------- #
# Artifact models
# --------------------------------------------------------------------------- #
