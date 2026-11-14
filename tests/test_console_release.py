"""Console-only publication must never include engine distributions."""

import json
from io import BytesIO
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tomllib
import zipfile

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
CONSOLE_VERSION = tomllib.loads(
    (ROOT / "packages/qwenpaw-data-console/pyproject.toml").read_text()
)["project"]["version"]
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
    "snow-leopard-loading.png", "snow-leopard-loading-dark.png",
    "snow-leopard-loading.mp4", "snow-leopard-loading-dark.mp4",
    "qwenpaw-data-brand.png", "mascot-blue-transparent.png",
    "fonts/google-sans-flex-latin-variable.woff2",
)


def write_distribution(directory, suffix, *, profiling=False, metadata_version=None, missing_asset=None, missing_module=None, changed_module=None, empty_asset=None, external_asset=None, requirements=(), requirement_kind=None, omit_config=None, deferred_config=None):
    name = "qwenpaw-data-" + suffix
    filename = name.replace("-", "_")
    def metadata(kind):
        return (
            f"Metadata-Version: 2.3\nName: {name}\n"
            f"Version: {metadata_version or CONSOLE_VERSION}\n"
            + "".join(f"Requires-Dist: {requirement}\n" for requirement in requirements
                      if requirement_kind in (None, kind))
            + "\n"
        ).encode()
    info = f"{filename}-{CONSOLE_VERSION}.dist-info/"
    wheel = directory / f"{filename}-{CONSOLE_VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(info + "METADATA", metadata("wheel"))
        archive.writestr(info + "WHEEL", "Wheel-Version: 1.0\n")
        archive.writestr(info + "RECORD", "")
        if suffix == "console":
            for app in ("chat", "databridge"):
                asset_root = f"qwenpaw_data/console/static/{app}/"
                asset_url = "/databridge/assets/main.js" if app == "databridge" else "/assets/main.js"
                extra = external_asset[2] if external_asset and external_asset[:2] == ("wheel", app) else ""
                config_script = '' if omit_config == ("wheel", app) else '<script src="/console-config.js"></script>'
                entry = f'<script type="module" src="{asset_url}"></script>{config_script}' if deferred_config == ("wheel", app) else f'{config_script}<script src="{asset_url}"></script>'
                archive.writestr(asset_root + "index.html", entry + extra)
                if missing_asset != ("wheel", app):
                    archive.writestr(asset_root + "assets/main.js", "" if empty_asset == ("wheel", app) else "export{};")
                if app in ("chat", "databridge"):
                    for asset in CONTEXT_RUNTIME_ASSETS if app == "databridge" else CHAT_RUNTIME_ASSETS:
                        runtime_kind = "wheel-runtime" if app == "databridge" else "wheel-chat-runtime"
                        if missing_asset != (runtime_kind, asset):
                            archive.writestr(asset_root + asset, "" if empty_asset == (runtime_kind, asset) else "asset")
        if profiling:
            root = ROOT / "packages/qwenpaw-data-context/src"
            for package in ("profiling", "context_manager", "semantic_config", "qwenpaw_data/context"):
                for path in (root / package).rglob("*.py"):
                    module = path.relative_to(root).as_posix()
                    if missing_module != ("wheel", module):
                        data = b"# altered\n" if changed_module == ("wheel", module) else path.read_bytes()
                        archive.writestr(module, data)
    sdist = directory / f"{filename}-{CONSOLE_VERSION}.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        member = tarfile.TarInfo(f"{filename}-{CONSOLE_VERSION}/PKG-INFO")
        sdist_metadata = metadata("sdist")
        member.size = len(sdist_metadata)
        archive.addfile(member, BytesIO(sdist_metadata))
        if profiling:
            root = ROOT / "packages/qwenpaw-data-context/src"
            for package in ("profiling", "context_manager", "semantic_config", "qwenpaw_data/context"):
                for path in (root / package).rglob("*.py"):
                    module = path.relative_to(root).as_posix()
                    if missing_module != ("sdist", module):
                        data = b"# altered\n" if changed_module == ("sdist", module) else path.read_bytes()
                        member = tarfile.TarInfo(f"{filename}-{CONSOLE_VERSION}/src/{module}")
                        member.size = len(data)
                        archive.addfile(member, BytesIO(data))
        if suffix == "console":
            for app in ("chat", "databridge"):
                asset_url = "/databridge/assets/main.js" if app == "databridge" else "/assets/main.js"
                asset_root = f"{filename}-{CONSOLE_VERSION}/src/qwenpaw_data/console/static/{app}/"
                extra = external_asset[2] if external_asset and external_asset[:2] == ("sdist", app) else ""
                config_script = '' if omit_config == ("sdist", app) else '<script src="/console-config.js"></script>'
                entry = f'<script type="module" src="{asset_url}"></script>{config_script}' if deferred_config == ("sdist", app) else f'{config_script}<script src="{asset_url}"></script>'
                for path, data in (("index.html", (entry + extra).encode()), ("assets/main.js", b"" if empty_asset == ("sdist", app) else b"export{};")):
                    if path.startswith("assets/") and missing_asset == ("sdist", app):
                        continue
                    asset = tarfile.TarInfo(asset_root + path)
                    asset.size = len(data)
                    archive.addfile(asset, BytesIO(data))
                if app in ("chat", "databridge"):
                    for path in CONTEXT_RUNTIME_ASSETS if app == "databridge" else CHAT_RUNTIME_ASSETS:
                        runtime_kind = "sdist-runtime" if app == "databridge" else "sdist-chat-runtime"
                        if missing_asset != (runtime_kind, path):
                            data = b"" if empty_asset == (runtime_kind, path) else b"asset"
                            asset = tarfile.TarInfo(asset_root + path)
                            asset.size = len(data)
                            archive.addfile(asset, BytesIO(data))
    return wheel, sdist


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
@pytest.mark.parametrize(("suffix", "requirement", "allowed"), [
    ("host-core", "qwenpaw_data_context>=0.4", False),
    ("context", "psycopg[binary]>=3", False),
    ("context", "psycopg[binary]>=3; python_version >= '3.12'", False),
    ("context", "psycopg[binary]>=3; extra == 'hologres'", True),
])
def test_release_checks_dependency_boundaries(tmp_path, suffix, requirement, allowed, kind):
    for item in ("console", "cli", "context", "host-core", "skills", "auth", "tracing"):
        write_distribution(
            tmp_path, item, profiling=item == "context",
            requirements=(requirement,) if item == suffix else (), requirement_kind=kind,
        )
    result = subprocess.run(
        [sys.executable, "scripts/check_release_versions.py", "--dist-dir", str(tmp_path)],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert (result.returncode == 0) is allowed, result.stderr
    if not allowed:
        assert ("Host must not depend on Context" if suffix == "host-core"
                else "psycopg dependency must be restricted to an extra") in result.stderr


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("console-v0.3.3", ["console"]),
        ("v0.4.0", ["console", "skills", "auth", "tracing", "context", "host-core", "cli"]),
        ("", ["console", "skills", "auth", "tracing", "context", "host-core", "cli"]),
    ],
)
def test_publication_scope(tag, expected, tmp_path):
    workflow = yaml.safe_load((ROOT / ".github/workflows/publish.yml").read_text())
    step = next(s for s in workflow["jobs"]["build"]["steps"] if s.get("id") == "scope")
    script = step["run"].split("python - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    output = tmp_path / "output"
    subprocess.run(
        [sys.executable, "-"], input=script, text=True, check=True,
        env={**os.environ, "RELEASE_TAG": tag, "GITHUB_OUTPUT": str(output)},
    )
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert json.loads(values["publish_matrix"]) == [
        {"project": "qwenpaw-data-" + suffix, "environment": "pypi-" + suffix}
        for suffix in expected
    ]
    assert values["package"] == ("qwenpaw-data-console" if len(expected) == 1 else "")


@pytest.mark.parametrize("engine_artifact", [False, True])
def test_console_dist_check_rejects_engine_artifacts(tmp_path, engine_artifact):
    write_distribution(tmp_path, "console")
    if engine_artifact:
        (tmp_path / f"qwenpaw_data_context-{CONSOLE_VERSION}-py3-none-any.whl").touch()
    result = subprocess.run(
        [sys.executable, "scripts/check_release_versions.py", f"console-v{CONSOLE_VERSION}",
         "--dist-dir", str(tmp_path)],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode == (1 if engine_artifact else 0), result.stderr
    if engine_artifact:
        assert "distribution set mismatch" in result.stderr
    else:
        assert "version and distribution validation passed" in result.stdout


def test_version_only_check_reports_distributions_were_not_checked():
    result = subprocess.run(
        [sys.executable, "scripts/check_release_versions.py"],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert "distributions were not checked" in result.stdout


def test_console_release_rejects_wrong_version():
    result = subprocess.run(
        [sys.executable, "scripts/check_release_versions.py", "console-v9.9.9"],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode == 1
    assert "does not match" in result.stderr


@pytest.mark.parametrize("missing", [None, "auth", "tracing"])
def test_full_release_requires_all_seven_packages(tmp_path, missing):
    suffixes = ["console", "skills", "auth", "tracing", "context", "host-core", "cli"]
    for suffix in suffixes:
        if suffix == missing:
            continue
        write_distribution(tmp_path, suffix, profiling=suffix == "context")
    result = subprocess.run(
        [sys.executable, "scripts/check_release_versions.py", "--dist-dir", str(tmp_path)],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode == (0 if missing is None else 1), result.stderr
    if missing is not None:
        assert "distribution set mismatch" in result.stderr


@pytest.mark.parametrize("invalid", [False, True])
def test_release_rejects_incomplete_context_wheel(tmp_path, invalid):
    for suffix in ("console", "skills", "auth", "tracing", "context", "host-core", "cli"):
        wheel, _ = write_distribution(tmp_path, suffix)
        if suffix == "context" and invalid:
            wheel.write_bytes(b"invalid wheel")
    result = subprocess.run(
        [sys.executable, "scripts/check_release_versions.py", "--dist-dir", str(tmp_path)],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode == 1
    assert ("invalid distribution" if invalid else "missing profiling modules") in result.stderr
    if not invalid:
        assert "include src/profiling" in result.stderr


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
@pytest.mark.parametrize("module", [
    "profiling/db.py",
    "context_manager/api/health.py",
    "semantic_config/main.py",
    "qwenpaw_data/context/__init__.py",
])
def test_release_rejects_missing_context_runtime_module(tmp_path, kind, module):
    for suffix in ("console", "skills", "auth", "tracing", "context", "host-core", "cli"):
        write_distribution(
            tmp_path, suffix, profiling=suffix == "context",
            missing_module=(kind, module),
        )
    result = subprocess.run(
        [sys.executable, "scripts/check_release_versions.py", "--dist-dir", str(tmp_path)],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode == 1
    assert module in result.stderr
    assert f"Context {kind}" in result.stderr


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_release_rejects_stale_context_runtime_module(tmp_path, kind):
    module = "context_manager/api/health.py"
    for suffix in ("console", "skills", "auth", "tracing", "context", "host-core", "cli"):
        write_distribution(
            tmp_path, suffix, profiling=suffix == "context",
            changed_module=(kind, module),
        )
    result = subprocess.run(
        [sys.executable, "scripts/check_release_versions.py", "--dist-dir", str(tmp_path)],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode == 1
    assert f"Context {kind}" in result.stderr
    assert "differing from the source tree" in result.stderr
    assert module in result.stderr


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_console_release_rejects_corrupt_distribution(tmp_path, kind):
    wheel, sdist = write_distribution(tmp_path, "console")
    (wheel if kind == "wheel" else sdist).write_bytes(b"not an archive")
    result = subprocess.run(
        [sys.executable, "scripts/check_release_versions.py", f"console-v{CONSOLE_VERSION}",
         "--dist-dir", str(tmp_path)], cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode == 1
    assert "invalid distribution" in result.stderr


def test_console_release_rejects_mismatched_metadata(tmp_path):
    write_distribution(tmp_path, "console", metadata_version="9.9.9")
    result = subprocess.run(
        [sys.executable, "scripts/check_release_versions.py", f"console-v{CONSOLE_VERSION}",
         "--dist-dir", str(tmp_path)], cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode == 1
    assert "metadata name or version does not match filename" in result.stderr


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
@pytest.mark.parametrize("app", ["chat", "databridge"])
def test_console_release_rejects_missing_frontend_asset(tmp_path, kind, app):
    write_distribution(tmp_path, "console", missing_asset=(kind, app))
    result = subprocess.run(
        [sys.executable, "scripts/check_release_versions.py", f"console-v{CONSOLE_VERSION}",
         "--dist-dir", str(tmp_path)], cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode == 1
    assert f"Console {app} references missing asset" in result.stderr


@pytest.mark.parametrize("kind", ["wheel-runtime", "sdist-runtime"])
def test_console_release_rejects_missing_context_runtime_asset(tmp_path, kind):
    asset = "snow-leopard-loading-dark.mp4"
    write_distribution(tmp_path, "console", missing_asset=(kind, asset))
    result = subprocess.run(
        [sys.executable, "scripts/check_release_versions.py", f"console-v{CONSOLE_VERSION}",
         "--dist-dir", str(tmp_path)], cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode == 1
    assert f"missing runtime assets: {asset}" in result.stderr


@pytest.mark.parametrize("kind", ["wheel-chat-runtime", "sdist-chat-runtime"])
def test_console_release_rejects_missing_chat_runtime_asset(tmp_path, kind):
    asset = "snow-leopard-loading-dark.mp4"
    write_distribution(tmp_path, "console", missing_asset=(kind, asset))
    result = subprocess.run(
        [sys.executable, "scripts/check_release_versions.py", "--package", "qwenpaw-data-console",
         "--dist-dir", str(tmp_path)], cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode != 0
    assert f"Console chat is missing runtime assets: {asset}" in result.stderr


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
@pytest.mark.parametrize("asset", ["chat", "databridge", "snow-leopard-loading-dark.mp4"])
def test_console_release_rejects_empty_frontend_asset(tmp_path, kind, asset):
    runtime = asset.endswith(".mp4")
    write_distribution(
        tmp_path, "console",
        empty_asset=(kind + "-runtime" if runtime else kind, asset),
    )
    result = subprocess.run(
        [sys.executable, "scripts/check_release_versions.py", f"console-v{CONSOLE_VERSION}",
         "--dist-dir", str(tmp_path)], cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode == 1
    assert "empty" in result.stderr
    assert (asset if runtime else "assets/main.js") in result.stderr


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
@pytest.mark.parametrize("app", ["chat", "databridge"])
@pytest.mark.parametrize("tag", [
    '<script src="https://example.com/code.js"></script>',
    '<link rel="stylesheet" href="//example.com/styles.css">',
])
def test_console_release_rejects_external_entry_assets(tmp_path, kind, app, tag):
    write_distribution(tmp_path, "console", external_asset=(kind, app, tag))
    result = subprocess.run(
        [sys.executable, "scripts/check_release_versions.py", f"console-v{CONSOLE_VERSION}",
         "--dist-dir", str(tmp_path)], cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode == 1
    assert f"Console {app} index.html references external asset" in result.stderr


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
@pytest.mark.parametrize("app", ["chat", "databridge"])
def test_console_release_rejects_missing_non_bundle_script(tmp_path, kind, app):
    write_distribution(tmp_path, "console", external_asset=(kind, app, '<script src="/missing.js"></script>'))
    result = subprocess.run(
        [sys.executable, "scripts/check_release_versions.py", "--package", "qwenpaw-data-console",
         "--dist-dir", str(tmp_path)], cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode != 0
    assert f"Console {app} references missing asset missing.js" in result.stderr


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
@pytest.mark.parametrize("app", ["chat", "databridge"])
def test_console_release_requires_config_before_bundle(tmp_path, kind, app):
    write_distribution(tmp_path, "console", omit_config=(kind, app))
    result = subprocess.run(
        [sys.executable, "scripts/check_release_versions.py", "--package", "qwenpaw-data-console",
         "--dist-dir", str(tmp_path)], cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode != 0
    assert f"Console {app} must load console-config.js before its bundle" in result.stderr


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
@pytest.mark.parametrize("app", ["chat", "databridge"])
def test_console_release_accepts_module_script_before_blocking_config(tmp_path, kind, app):
    write_distribution(tmp_path, "console", deferred_config=(kind, app))
    result = subprocess.run(
        [sys.executable, "scripts/check_release_versions.py", "--package", "qwenpaw-data-console",
         "--dist-dir", str(tmp_path)], cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
@pytest.mark.parametrize("app", ["chat", "databridge"])
def test_console_release_allows_dynamic_config_script(tmp_path, kind, app):
    write_distribution(tmp_path, "console", external_asset=(kind, app, '<script src="/console-config.js"></script>'))
    result = subprocess.run(
        [sys.executable, "scripts/check_release_versions.py", "--package", "qwenpaw-data-console",
         "--dist-dir", str(tmp_path)], cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("suffix", ["auth", "tracing"])
def test_console_release_rejects_new_engine_distributions(tmp_path, suffix):
    for name in (
        f"qwenpaw_data_console-{CONSOLE_VERSION}-py3-none-any.whl",
        f"qwenpaw_data_console-{CONSOLE_VERSION}.tar.gz",
        f"qwenpaw_data_{suffix}-{CONSOLE_VERSION}-py3-none-any.whl",
    ):
        (tmp_path / name).touch()
    result = subprocess.run(
        [sys.executable, "scripts/check_release_versions.py", f"console-v{CONSOLE_VERSION}",
         "--dist-dir", str(tmp_path)],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode == 1
    assert "distribution set mismatch" in result.stderr
