#!/usr/bin/env python3
"""Audit every Git ref for non-public identities and historical terms."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args],
        cwd=ROOT,
        text=True,
        errors="replace",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--forbidden-email-domain", action="append", default=[])
    parser.add_argument("--forbidden-term", action="append", default=[])
    args = parser.parse_args()
    if not args.forbidden_email_domain and not args.forbidden_term:
        parser.error("provide at least one forbidden domain or term")

    findings: list[str] = []
    identities = _git(
        "log",
        "--all",
        "--format=%H%x09%ae%x09%ce",
    ).splitlines()
    for line in identities:
        commit, author, committer = line.split("\t", 2)
        for domain in args.forbidden_email_domain:
            normalized = domain.casefold().lstrip("@")
            if author.casefold().endswith("@" + normalized):
                findings.append(f"{commit}: forbidden author email {author}")
            if committer.casefold().endswith("@" + normalized):
                findings.append(f"{commit}: forbidden committer email {committer}")

    for term in args.forbidden_term:
        commits = _git(
            "log",
            "--all",
            "--format=%H",
            "-i",
            "-S",
            term,
        ).splitlines()
        findings.extend(
            f"{commit}: patch history contains term {term!r}" for commit in commits
        )

    if findings:
        print("public-history audit failed:")
        for finding in sorted(set(findings)):
            print(f"- {finding}")
        return 1
    print("public-history audit passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
