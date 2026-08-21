#!/usr/bin/env python3
"""Validate monorepo versions and the expected release distribution set."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PROJECTS = tuple(sorted((ROOT / "packages").glob("*/pyproject.toml")))
DIST_NAME = re.compile(
    r"^(?P<name>qwenpaw[-_]data(?:[-_](?:cli|context|host[-_]core|skills)))"
    r"-(?P<version>\d+\.\d+\.\d+)(?:-|\.)",
)


def project_versions() -> dict[str, str]:
    projects = [ROOT / "pyproject.toml", *PACKAGE_PROJECTS]
    return {
        metadata["name"]: metadata["version"]
        for project in projects
        for metadata in [tomllib.loads(project.read_text(encoding="utf-8"))["project"]]
    }


def normalize_tag(tag: str) -> str:
    value = tag.removeprefix("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError(f"release tag must be vMAJOR.MINOR.PATCH: {tag!r}")
    return value


def validate_versions(expected: str | None) -> str:
    versions = project_versions()
    unique = set(versions.values())
    if len(unique) != 1:
        details = ", ".join(f"{name}={version}" for name, version in versions.items())
        raise ValueError(f"workspace versions are not aligned: {details}")
    version = unique.pop()
    if expected is not None and version != expected:
        raise ValueError(f"release tag version {expected} does not match workspace {version}")
    return version


def validate_distributions(dist_dir: Path, version: str) -> None:
    expected_names = {
        "qwenpaw-data-cli",
        "qwenpaw-data-context",
        "qwenpaw-data-host-core",
        "qwenpaw-data-skills",
    }
    found: dict[str, set[str]] = {}
    for path in dist_dir.iterdir():
        match = DIST_NAME.match(path.name)
        if match is None:
            continue
        name = match.group("name").replace("_", "-")
        found.setdefault(name, set()).add(path.suffix)
        if match.group("version") != version:
            raise ValueError(f"distribution version mismatch: {path.name}")
    if set(found) != expected_names:
        raise ValueError(
            "distribution set mismatch: "
            f"expected {sorted(expected_names)}, found {sorted(found)}",
        )
    incomplete = sorted(name for name, suffixes in found.items() if ".whl" not in suffixes or ".gz" not in suffixes)
    if incomplete:
        raise ValueError(f"missing wheel or sdist for: {', '.join(incomplete)}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", nargs="?", help="Release tag, for example v0.1.0")
    parser.add_argument("--dist-dir", type=Path, help="Validate built wheels and sdists")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        expected = normalize_tag(args.tag) if args.tag else None
        version = validate_versions(expected)
        if args.dist_dir:
            validate_distributions(args.dist_dir.resolve(), version)
    except (OSError, ValueError) as exc:
        print(f"release validation failed: {exc}", file=sys.stderr)
        return 1
    print(f"release validation passed for {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
