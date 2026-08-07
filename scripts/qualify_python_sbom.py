#!/usr/bin/env python3
"""Add ecosystem-qualified PyPI package URLs to a CycloneDX SBOM."""

from __future__ import annotations

import argparse
import json
import re
from importlib.metadata import distributions
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote


def pypi_purl(name: str, version: str) -> str:
    """Return the canonical Package URL for an installed PyPI distribution."""
    normalized_name = name.lower().replace("_", "-")
    encoded_name = quote(normalized_name, safe=".-~")
    encoded_version = quote(version, safe=".-_~")
    return f"pkg:pypi/{encoded_name}@{encoded_version}"


def normalized_distribution_name(name: str) -> str:
    """Return a PEP 503 name for matching installed distribution metadata."""
    return re.sub(r"[-_.]+", "-", name).lower()


def installed_license_expressions() -> dict[str, str]:
    """Collect standardized PEP 639 license expressions from the environment."""
    expressions: dict[str, str] = {}
    for distribution in distributions():
        name = distribution.metadata.get("Name")
        expression = distribution.metadata.get("License-Expression")
        if name and expression:
            expressions[normalized_distribution_name(name)] = expression
    return expressions


def qualify_components(
    bom: dict[str, Any],
    license_expressions: Mapping[str, str] | None = None,
) -> int:
    """Annotate every component in a Python-only SBOM and return the count."""
    components = bom.get("components")
    if not isinstance(components, list):
        raise ValueError("CycloneDX document must contain a components array")

    for index, component in enumerate(components):
        if not isinstance(component, dict):
            raise ValueError(f"component {index} must be an object")
        name = component.get("name")
        version = component.get("version")
        if not isinstance(name, str) or not name:
            raise ValueError(f"component {index} must have a non-empty name")
        if not isinstance(version, str) or not version:
            raise ValueError(f"component {index} must have a non-empty version")
        component["purl"] = pypi_purl(name, version)
        if license_expressions and "licenses" not in component:
            expression = license_expressions.get(normalized_distribution_name(name))
            if expression:
                component["licenses"] = [{"expression": expression}]

    return len(components)


def qualify_file(
    path: Path,
    license_expressions: Mapping[str, str] | None = None,
) -> int:
    bom = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(bom, dict):
        raise ValueError("CycloneDX document must be a JSON object")
    count = qualify_components(bom, license_expressions)
    path.write_text(f"{json.dumps(bom, indent=2)}\n", encoding="utf-8")
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sbom", type=Path, help="CycloneDX JSON file to update")
    parser.add_argument(
        "--from-environment",
        action="store_true",
        help="add installed packages' PEP 639 license expressions",
    )
    args = parser.parse_args()
    expressions = installed_license_expressions() if args.from_environment else None
    count = qualify_file(args.sbom, expressions)
    message = f"Added PyPI PURLs to {count} SBOM components"
    if expressions is not None:
        message += f" and loaded {len(expressions)} PEP 639 license expressions"
    print(message)


if __name__ == "__main__":
    main()
