#!/usr/bin/env python3
"""Validate monorepo versions and the expected release distribution set."""

from __future__ import annotations

import argparse
from email.parser import BytesParser
from html.parser import HTMLParser
import posixpath
import re
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PROJECTS = tuple(sorted((ROOT / "packages").glob("*/pyproject.toml")))
DIST_NAME = re.compile(
    r"^(?P<name>qwenpaw[-_]data(?:[-_](?:cli|console|context|host[-_]core|skills|auth|tracing)))"
    r"-(?P<version>\d+\.\d+\.\d+)(?:-|\.)",
)
CONTEXT_RUNTIME_ASSETS = (
    "snow-leopard-loading.png",
    "snow-leopard-loading-dark.png",
    "snow-leopard-loading.mp4",
    "snow-leopard-loading-dark.mp4",
    "qwenpaw-data-brand.png",
    "qwenpaw-data-wordmark.png",
    "fonts/google-sans-flex-latin-variable.woff2",
)
CHAT_RUNTIME_ASSETS = (
    "snow-leopard-loading.png",
    "snow-leopard-loading-dark.png",
    "snow-leopard-loading.mp4",
    "snow-leopard-loading-dark.mp4",
    "qwenpaw-data-brand.png",
    "mascot-blue-transparent.png",
    "fonts/google-sans-flex-latin-variable.woff2",
)


class _AssetReferences(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.references: list[tuple[str, str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        value = attributes.get("src" if tag == "script" else "href" if tag == "link" else "")
        if value:
            self.references.append((tag, value, attributes))


def _validate_console_assets(names: set[str], read, prefix: str) -> None:
    for app in ("chat", "databridge"):
        root = f"{prefix}qwenpaw_data/console/static/{app}/"
        index = root + "index.html"
        if index not in names:
            raise ValueError(f"Console {app} index.html is missing")
        runtime_assets = CONTEXT_RUNTIME_ASSETS if app == "databridge" else CHAT_RUNTIME_ASSETS
        missing = [path for path in runtime_assets if root + path not in names]
        if missing:
            raise ValueError(f"Console {app} is missing runtime assets: " + ", ".join(missing))
        empty = [path for path in runtime_assets if not read(root + path)]
        if empty:
            raise ValueError(f"Console {app} has empty runtime assets: " + ", ".join(empty))
        parser = _AssetReferences()
        parser.feed(read(index).decode("utf-8"))
        bundles = []
        config_loaded = False
        early_bundle = False
        for tag, reference, attributes in parser.references:
            parsed = urlsplit(reference)
            if parsed.netloc or parsed.scheme.lower() in {"http", "https"}:
                raise ValueError(f"Console {app} index.html references external asset")
            if parsed.scheme:
                if tag == "script":
                    raise ValueError(f"Console {app} index.html references non-local script")
                continue
            path = unquote(parsed.path)
            if app == "databridge" and path.startswith("/databridge/"):
                path = path.removeprefix("/databridge/")
            else:
                path = path.lstrip("/")
            path = posixpath.normpath(path)
            if tag == "script" and path == "console-config.js":
                # Served dynamically by the Console application, not packaged as static data.
                if "async" in attributes or "defer" in attributes or (attributes.get("type") or "").lower() == "module":
                    raise ValueError(f"Console {app} config script must execute synchronously")
                config_loaded = True
                continue
            if not path.startswith("assets/") and tag != "script":
                continue
            if root + path not in names:
                raise ValueError(f"Console {app} references missing asset {path}")
            if not read(root + path):
                raise ValueError(f"Console {app} references empty asset {path}")
            if path.startswith("assets/"):
                if tag == "script" and path.endswith(".js") and not config_loaded:
                    if "async" in attributes or ("defer" not in attributes and (attributes.get("type") or "").lower() != "module"):
                        early_bundle = True
                bundles.append(path)
        if not any(item.endswith(".js") for item in bundles):
            raise ValueError(f"Console {app} index.html has no bundled JavaScript")
        if not config_loaded or early_bundle:
            raise ValueError(f"Console {app} must load console-config.js before its bundle")


def project_versions() -> dict[str, str]:
    projects = [ROOT / "pyproject.toml", *PACKAGE_PROJECTS]
    return {
        metadata["name"]: metadata["version"]
        for project in projects
        for metadata in [tomllib.loads(project.read_text(encoding="utf-8"))["project"]]
    }


def normalize_tag(tag: str) -> str:
    value = tag.removeprefix("console-").removeprefix("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError(f"release tag must be vMAJOR.MINOR.PATCH: {tag!r}")
    return value


def _validate_dependency_boundaries(name: str, info) -> None:
    for requirement in info.get_all("Requires-Dist", []):
        dependency, _, marker = requirement.partition(";")
        match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", dependency)
        if match is None:
            raise ValueError(f"invalid dependency metadata: {requirement}")
        dependency_name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
        if name == "qwenpaw-data-host-core" and dependency_name == "qwenpaw-data-context":
            raise ValueError("Host must not depend on Context")
        if name == "qwenpaw-data-context" and dependency_name == "psycopg":
            # Accept only a simple extra marker. More complicated expressions are
            # rejected rather than guessing whether the dependency is optional.
            if not re.fullmatch(r'''\s*extra\s*==\s*(["'])[^"']+\1\s*''', marker):
                raise ValueError("Context psycopg dependency must be restricted to an extra")


def validate_versions(expected: str | None, package: str | None = None) -> str:
    versions = project_versions()
    if package:
        versions = {package: versions[package]}
    unique = set(versions.values())
    if len(unique) != 1:
        details = ", ".join(f"{name}={version}" for name, version in versions.items())
        raise ValueError(f"workspace versions are not aligned: {details}")
    version = unique.pop()
    if expected is not None and version != expected:
        raise ValueError(f"release tag version {expected} does not match workspace {version}")
    return version


def validate_distributions(dist_dir: Path, version: str, package: str | None = None) -> None:
    expected_names = {
        "qwenpaw-data-console",
        "qwenpaw-data-cli",
        "qwenpaw-data-context",
        "qwenpaw-data-host-core",
        "qwenpaw-data-skills",
        "qwenpaw-data-auth",
        "qwenpaw-data-tracing",
    }
    if package:
        expected_names = {package}
    found: dict[str, set[str]] = {}
    context_wheels: list[Path] = []
    context_sdists: list[Path] = []
    for path in dist_dir.iterdir():
        match = DIST_NAME.match(path.name)
        if match is None:
            continue
        name = match.group("name").replace("_", "-")
        if not (path.name.endswith(".whl") or path.name.endswith(".tar.gz")):
            continue
        found.setdefault(name, set()).add(path.suffix)
        if name == "qwenpaw-data-context" and path.suffix == ".whl":
            context_wheels.append(path)
        if name == "qwenpaw-data-context" and path.name.endswith(".tar.gz"):
            context_sdists.append(path)
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

    for path in dist_dir.iterdir():
        match = DIST_NAME.match(path.name)
        if match is None or not (path.name.endswith(".whl") or path.name.endswith(".tar.gz")):
            continue
        name = match.group("name").replace("_", "-")
        try:
            if path.name.endswith(".whl"):
                with zipfile.ZipFile(path) as archive:
                    members = archive.namelist()
                    metadata = [item for item in members if item.endswith(".dist-info/METADATA")]
                    if len(metadata) != 1:
                        raise ValueError("wheel must contain one METADATA file")
                    base = metadata[0].removesuffix("METADATA")
                    if base + "WHEEL" not in members or base + "RECORD" not in members:
                        raise ValueError("wheel is missing WHEEL or RECORD")
                    payload = archive.read(metadata[0])
                    if name == "qwenpaw-data-console":
                        _validate_console_assets(set(members), archive.read, "")
            else:
                with tarfile.open(path, "r:gz") as archive:
                    metadata = [item for item in archive.getmembers() if item.isfile() and item.name.count("/") == 1 and item.name.endswith("/PKG-INFO")]
                    if len(metadata) != 1:
                        raise ValueError("sdist must contain one root PKG-INFO file")
                    stream = archive.extractfile(metadata[0])
                    if stream is None:
                        raise ValueError("cannot read sdist PKG-INFO")
                    payload = stream.read()
                    if name == "qwenpaw-data-console":
                        def read_member(member_name: str) -> bytes:
                            member = archive.extractfile(member_name)
                            if member is None:
                                raise ValueError(f"cannot read {member_name}")
                            return member.read()

                        root = metadata[0].name.split("/", 1)[0]
                        _validate_console_assets(
                            set(archive.getnames()), read_member, root + "/src/"
                        )
            info = BytesParser().parsebytes(payload)
            if (info.get("Name", "").lower().replace("_", "-") != name
                    or info.get("Version") != version):
                raise ValueError("distribution metadata name or version does not match filename")
            _validate_dependency_boundaries(name, info)
        except (OSError, tarfile.TarError, zipfile.BadZipFile, ValueError) as exc:
            raise ValueError(f"invalid distribution {path.name}: {exc}") from exc

    # Both installation paths must contain every Context runtime module.
    source = ROOT / "packages/qwenpaw-data-context/src"
    for package_path in ("profiling", "context_manager", "semantic_config", "qwenpaw_data/context"):
        package_root = source / package_path
        expected = {
            f"{package_path}/{path.relative_to(package_root).as_posix()}"
            for path in package_root.rglob("*.py")
        }
        if (context_wheels or context_sdists) and not expected:
            raise ValueError(f"Context {package_path} source package is missing")
        for artifact in (*context_wheels, *context_sdists):
            if artifact.suffix == ".whl":
                with zipfile.ZipFile(artifact) as archive:
                    members = set(archive.namelist())
                prefix = ""
                kind = "wheel"
            else:
                with tarfile.open(artifact, "r:gz") as archive:
                    members = set(archive.getnames())
                prefix = artifact.name.removesuffix(".tar.gz") + "/src/"
                kind = "sdist"
            missing = sorted(path for path in expected if prefix + path not in members)
            if missing:
                message = (
                    f"Context {kind} {artifact.name} is missing {package_path} modules: "
                    + ", ".join(missing[:5])
                    + (f" (and {len(missing) - 5} more)" if len(missing) > 5 else "")
                )
                if kind == "wheel" and package_path == "profiling":
                    message += (
                        "; include src/profiling in packages/qwenpaw-data-context/pyproject.toml "
                        "[tool.hatch.build.targets.wheel].packages"
                    )
                raise ValueError(message)
            if kind == "wheel":
                with zipfile.ZipFile(artifact) as archive:
                    changed = sorted(
                        path for path in expected
                        if archive.read(path) != (source / path).read_bytes()
                    )
            else:
                with tarfile.open(artifact, "r:gz") as archive:
                    changed = []
                    lookup = {prefix + path: path for path in expected}
                    for member in archive:
                        path = lookup.get(member.name)
                        if path is not None:
                            stream = archive.extractfile(member)
                            if stream is None or stream.read() != (source / path).read_bytes():
                                changed.append(path)
                    changed.sort()
            if changed:
                raise ValueError(
                    f"Context {kind} {artifact.name} has modules differing from the source tree: "
                    + ", ".join(changed[:5])
                    + (f" (and {len(changed) - 5} more)" if len(changed) > 5 else "")
                )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", nargs="?", help="Release tag: vX.Y.Z or console-vX.Y.Z")
    parser.add_argument("--package", choices=["qwenpaw-data-console"], help="Validate only the Console distribution")
    parser.add_argument("--dist-dir", type=Path, help="Validate built wheels and sdists")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        package = args.package
        if args.tag and args.tag.startswith("console-"):
            package = "qwenpaw-data-console"
        expected = normalize_tag(args.tag) if args.tag else None
        version = validate_versions(expected, package)
        if args.dist_dir:
            validate_distributions(args.dist_dir.resolve(), version, package)
    except (OSError, ValueError) as exc:
        print(f"release validation failed: {exc}", file=sys.stderr)
        return 1
    if args.dist_dir:
        print(f"version and distribution validation passed for {version}")
    else:
        print(f"version validation passed for {version}; distributions were not checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
