#!/usr/bin/env python3
"""
Explore all files in a data directory.
Profiles CSV/TSV and Excel workbooks, prioritizing README/metadata sheets,
and prints a raw preview so true headers and metadata rows are visible.

Usage:
    python explore_input.py [data_dir]

data_dir defaults to "input" but can be any directory the task provides.
"""

import sys
import pandas as pd
from pathlib import Path

PRIORITY_KEYWORDS = ["readme", "legend", "metadata", "description", "info", "dictionary"]


def raw_preview(filepath, nlines=15):
    """Print the first N raw lines so metadata blocks / true headers are visible."""
    try:
        with open(filepath, "r", encoding="utf-8-sig", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= nlines:
                    break
                print(f"  raw[{i}]: {line.rstrip()}")
    except Exception as e:
        print(f"  (raw preview failed: {e})")


def explore_table(filepath, sep=None, nrows=5):
    """Profile a delimited text file (CSV/TSV)."""
    print(f"\n{'='*60}\nTABLE: {filepath}\n{'='*60}")
    raw_preview(filepath)
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            df = pd.read_csv(filepath, sep=sep, engine="python", nrows=nrows, encoding=encoding)
            print(f"\nParsed (encoding={encoding}) shape (first {nrows} rows): {df.shape}")
            print(f"Columns ({len(df.columns)}):")
            for i, col in enumerate(df.columns):
                print(f"  {i}: {col} (dtype: {df[col].dtype})")
            print(f"First {nrows} rows:\n{df.head()}")
            return
        except Exception as e:
            last_err = e
    print(f"Error reading table: {last_err}")


def explore_excel(filepath, nrows=5):
    """Profile an Excel workbook, prioritizing README/metadata sheets."""
    print(f"\n{'='*60}\nEXCEL: {filepath}\n{'='*60}")
    try:
        xls = pd.ExcelFile(filepath)
    except Exception as e:
        print(f"Error opening Excel file: {e}")
        return

    print(f"Sheet names: {xls.sheet_names}")
    priority = [s for s in xls.sheet_names if any(k in s.lower() for k in PRIORITY_KEYWORDS)]
    data_sheets = [s for s in xls.sheet_names if s not in priority]

    for sheet in priority + data_sheets:
        print(f"\n--- Sheet: {sheet} ---")
        try:
            df = pd.read_excel(xls, sheet_name=sheet, nrows=nrows)
            print(f"Shape (first {nrows} rows): {df.shape}")
            print(f"Columns ({len(df.columns)}):")
            for i, col in enumerate(df.columns):
                print(f"  {i}: {col} (dtype: {df[col].dtype})")
            print(f"First {nrows} rows:\n{df.head()}")
        except Exception as e:
            print(f"Error reading sheet: {e}")


def main():
    data_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("input")
    if not data_dir.exists():
        print(f"No '{data_dir}' directory found")
        return

    files = sorted(p for p in data_dir.glob("**/*") if p.is_file())
    print(f"Found {len(files)} files in {data_dir}/")

    for filepath in files:
        suffix = filepath.suffix.lower()
        if suffix == ".csv":
            explore_table(filepath, sep=",")
        elif suffix in (".tsv", ".tab"):
            explore_table(filepath, sep="\t")
        elif suffix in (".xlsx", ".xls", ".xlsm"):
            explore_excel(filepath)
        elif suffix in (".txt", ".dat"):
            explore_table(filepath, sep=None)
        else:
            print(f"\nSkipping unsupported format: {filepath}")


if __name__ == "__main__":
    main()
