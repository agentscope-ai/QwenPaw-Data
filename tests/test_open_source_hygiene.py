from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _tracked_paths() -> list[Path]:
    output = subprocess.check_output(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
    )
    return [ROOT / item.decode() for item in output.split(b"\0") if item]


def test_tracked_tree_has_no_internal_brand_or_network_markers() -> None:
    markers = (
        "qwen" + "chat",
        "bai" + "lian",
        "alibaba" + "-inc.com",
        "gitlab." + "alibaba",
        "tao" + "bao",
    )
    binary_suffixes = {
        ".docx",
        ".gif",
        ".ico",
        ".jpeg",
        ".jpg",
        ".otf",
        ".pdf",
        ".png",
        ".ttf",
        ".webp",
        ".woff",
        ".woff2",
        ".xls",
        ".xlsx",
    }
    failures: list[str] = []
    for path in _tracked_paths():
        relative = path.relative_to(ROOT).as_posix().lower()
        if any(marker in relative for marker in markers):
            failures.append(relative)
            continue
        if path.suffix.lower() in binary_suffixes:
            continue
        content = path.read_text(encoding="utf-8", errors="ignore").lower()
        if any(marker in content for marker in markers):
            failures.append(relative)
    assert failures == []


def test_xlsx_notice_matches_locked_registry_package() -> None:
    package_json = json.loads(
        (ROOT / "packages/qwenpaw-data-context/frontend/package.json").read_text(encoding="utf-8")
    )
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    assert package_json["dependencies"]["xlsx"] == "npm:@e965/xlsx@0.20.3"
    assert '"@e965/xlsx" version 0.20.3' in notice
    assert "cdn.sheetjs.com" not in notice


def test_formal_api_has_no_declared_501_stub_routes() -> None:
    from context_manager.api.semantic_api import router

    routes = [
        route.path
        for route in router.routes
        if getattr(route, "status_code", None) == 501
    ]
    assert routes == []


def test_all_python_projects_declare_apache_license() -> None:
    projects = [ROOT / "pyproject.toml", *sorted((ROOT / "packages").glob("*/pyproject.toml"))]
    for project in projects:
        metadata = tomllib.loads(project.read_text(encoding="utf-8"))["project"]
        assert metadata["license"] == "Apache-2.0", project


def test_notice_covers_repository_assets_and_examples() -> None:
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    assert "Repository assets and examples" in notice
    assert "ASSET_LICENSES.md" in notice


def test_release_baseline_is_documented() -> None:
    assert (ROOT / "CHANGELOG.md").is_file()
    assert (ROOT / "docs/COMPATIBILITY.md").is_file()
    assert (ROOT / "docs/RELEASING.md").is_file()
    if os.name != "nt":  # Windows checkouts do not track POSIX exec bits
        assert (ROOT / "scripts/export_public_snapshot.sh").stat().st_mode & 0o111
        assert (ROOT / "scripts/audit_git_history.py").stat().st_mode & 0o111


def test_all_python_projects_have_public_package_metadata() -> None:
    projects = [ROOT / "pyproject.toml", *sorted((ROOT / "packages").glob("*/pyproject.toml"))]
    required_urls = {"Homepage", "Documentation", "Repository", "Issues", "Changelog"}
    for project in projects:
        metadata = tomllib.loads(project.read_text(encoding="utf-8"))["project"]
        assert metadata.get("authors"), project
        assert metadata.get("keywords"), project
        assert "License :: OSI Approved :: Apache Software License" in metadata.get("classifiers", []), project
        assert required_urls <= set(metadata.get("urls", {})), project


def test_publishable_packages_ship_root_license_and_notice() -> None:
    for project in sorted((ROOT / "packages").glob("*/pyproject.toml")):
        config = tomllib.loads(project.read_text(encoding="utf-8"))
        forced = config["tool"]["hatch"]["build"]["targets"]["sdist"]["force-include"]
        assert forced == {"../../LICENSE": "LICENSE", "../../NOTICE": "NOTICE"}, project


def test_community_health_files_are_present() -> None:
    paths = [
        ROOT / "SUPPORT.md",
        ROOT / ".github/PULL_REQUEST_TEMPLATE.md",
        ROOT / ".github/ISSUE_TEMPLATE/config.yml",
        ROOT / ".github/ISSUE_TEMPLATE/bug_report.yml",
        ROOT / ".github/ISSUE_TEMPLATE/feature_request.yml",
    ]
    assert all(path.is_file() and path.stat().st_size > 0 for path in paths)


def test_publish_workflow_uses_trusted_publishing() -> None:
    workflow = (ROOT / ".github/workflows/publish.yml").read_text(encoding="utf-8")
    assert "id-token: write" in workflow
    assert "pypa/gh-action-pypi-publish@release/v1" in workflow
    assert "PYPI_API_TOKEN" not in workflow
    assert "secrets." not in workflow
    assert "if: github.event_name == 'release'" in workflow


def test_release_versions_are_aligned() -> None:
    workspace_version = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/check_release_versions.py",
            f"v{workspace_version}",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
