#!/usr/bin/env python3
"""Render the NL2SQL prompt from the structured problem and context.json.

``context.json`` is ``{"metrics": {...}, "tables": {...}}``; see SKILL.md Step 4,
"context.json", for the format.

Does not call LLM or MCP.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from _common import (
    EXIT_OK,
    add_common_args,
    fatal,
    read_json,
    render_prompt,
    setup_logging,
    write_text,
)

MAX_VALUES_SHOWN = 10
NONE_BLOCK = "``(none)``"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--problem", required=True, help="Structured problem JSON (Step 1)")
    p.add_argument("--context", required=True, help="context.json ({metrics, tables})")
    add_common_args(p)
    return p.parse_args(argv)


def _one_line(value: Any) -> str:
    return str(value).replace("\n", " ").strip() if value else ""


def render_metrics(metrics: dict[str, Any] | None) -> str:
    """Markdown block consumed by ``{{metrics}}`` in the prompt template."""
    if not metrics:
        return NONE_BLOCK

    parts: list[str] = []
    for name, m in metrics.items():
        head = f"- **{name}**"
        if str(m.get("priority") or "").lower() == "high":
            head += "  (priority=high)"
        if m.get("alias"):
            head += f"  aliases={m['alias']}"
        if m.get("src"):
            head += f"  src={m['src']}"
        parts.append(head)
        if m.get("desc"):
            parts.append(f"  - description: {_one_line(m['desc'])}")

        for c in m.get("calibers") or []:
            sub = f"  - dataset=`{c.get('table', '?')}`"
            if c.get("window"):
                sub += f"  time_window={c['window']}"
            if c.get("additivity"):
                sub += f"  additivity={c['additivity']}"
            if c.get("default") is True:
                sub += "  is_default=true"
            parts.append(sub)
            if c.get("expr"):
                parts.append(f"      formula: {_one_line(c['expr'])}")
    return "\n".join(parts).rstrip() + "\n"


def _render_col(name: str, col: list[Any]) -> str | None:
    """One column ``[dtype, desc, values?]``; None when it carries no information."""
    dtype = col[0] if len(col) > 0 else None
    desc = _one_line(col[1]) if len(col) > 1 else ""
    values = col[2] if len(col) > 2 and col[2] else []
    if not dtype and not desc:  # graph noise, e.g. a field named `where`
        return None

    line = f"  - `{name}` ({dtype or '?'})"
    if desc:
        line += f" — {desc}"
    if values:
        shown = values[:MAX_VALUES_SHOWN]
        more = (
            f", ...(+{len(values) - MAX_VALUES_SHOWN})"
            if len(values) > MAX_VALUES_SHOWN
            else ""
        )
        line += f"  values={shown}{more}"
    return line


def render_schema(tables: dict[str, Any] | None) -> str:
    """Markdown block consumed by ``{{schema}}`` in the prompt template."""
    if not tables:
        return NONE_BLOCK

    parts: list[str] = []
    for name, t in tables.items():
        parts.append(f"### Table: {name}")
        if t.get("desc"):
            parts.append(f"- description: {_one_line(t['desc'])}")
        if t.get("note"):
            parts.append(f"- note: {_one_line(t['note'])}")
        parts.append("- columns:")
        for col_name, col in (t.get("cols") or {}).items():
            line = _render_col(col_name, col)
            if line:
                parts.append(line)
        parts.append("")  # blank line between tables
    return "\n".join(parts).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logger = setup_logging(args.log_level)

    try:
        question = read_json(args.problem).get("question", "")
        context = read_json(args.context)
        prompt_text = render_prompt(
            "nl2sql.md",
            schema=render_schema(context.get("tables")),
            metrics=render_metrics(context.get("metrics")),
            question=question,
        )
    except Exception as e:
        fatal(logger, str(e))
        return 1

    write_text(prompt_text, args.output)
    logger.info("Wrote NL2SQL prompt (%d chars)", len(prompt_text))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
