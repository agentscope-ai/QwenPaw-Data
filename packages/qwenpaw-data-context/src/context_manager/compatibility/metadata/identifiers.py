"""Normalize physical table references for Metadata lookup."""


def logical_dataset_name(
    table_or_qualified: str,
) -> str:
    """Resolve a physical table reference to its logical Dataset name."""
    raw = (table_or_qualified or "").strip()
    if not raw:
        return ""
    # Already has a view prefix — keep as-is (build-type filtered datasets)
    if raw.startswith("view_"):
        return raw
    if "." in raw:
        try:
            _ns, table = split_qualified_table(raw)
        except ValueError:
            table = raw.rsplit(".", 1)[-1]
    else:
        table = raw
    return table


def split_qualified_table(qualified: str) -> tuple[str, str]:
    """Split a two-part project/schema and table reference."""
    parts = qualified.split(".")
    if len(parts) != 2:
        raise ValueError(f"expected '<ns>.<table>' (2 segments), got: {qualified!r}")
    ns, table = parts
    return ns, table
