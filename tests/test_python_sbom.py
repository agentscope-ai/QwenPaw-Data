from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_sbom_module() -> ModuleType:
    path = ROOT / "scripts" / "qualify_python_sbom.py"
    spec = importlib.util.spec_from_file_location("qwenpaw_data_python_sbom_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pypi_purl_qualifies_ecosystem_and_normalizes_name() -> None:
    sbom_module = _load_sbom_module()
    assert (
        sbom_module.pypi_purl("OpenTelemetry_API", "1.43.0+local")
        == "pkg:pypi/opentelemetry-api@1.43.0%2Blocal"
    )


def test_qualify_components_adds_standardized_license_expression() -> None:
    sbom_module = _load_sbom_module()
    bom = {
        "components": [
            {
                "bom-ref": "BomRef.123",
                "name": "opentelemetry-api",
                "type": "library",
                "version": "1.43.0",
            }
        ]
    }

    assert (
        sbom_module.qualify_components(
            bom,
            {"opentelemetry-api": "Apache-2.0"},
        )
        == 1
    )
    assert bom["components"][0] == {
        "bom-ref": "BomRef.123",
        "licenses": [{"expression": "Apache-2.0"}],
        "name": "opentelemetry-api",
        "purl": "pkg:pypi/opentelemetry-api@1.43.0",
        "type": "library",
        "version": "1.43.0",
    }


def test_qualify_components_preserves_existing_license() -> None:
    sbom_module = _load_sbom_module()
    bom = {
        "components": [
            {
                "licenses": [{"expression": "BSD-3-Clause"}],
                "name": "networkx",
                "version": "3.6.1",
            }
        ]
    }

    sbom_module.qualify_components(bom, {"networkx": "MIT"})

    assert bom["components"][0]["licenses"] == [
        {"expression": "BSD-3-Clause"}
    ]


def test_qualify_file_rejects_ambiguous_components(tmp_path) -> None:
    sbom_module = _load_sbom_module()
    sbom = tmp_path / "python.cdx.json"
    sbom.write_text(json.dumps({"components": [{"name": "missing-version"}]}))

    with pytest.raises(ValueError, match="non-empty version"):
        sbom_module.qualify_file(sbom)
