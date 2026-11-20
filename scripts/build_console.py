#!/usr/bin/env python3
"""Build both Console UIs once for a Python wheel; end users need no Node.js."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "packages/qwenpaw-data-console"
STATIC = PACKAGE / "src/qwenpaw_data/console/static"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-install", action="store_true", help="Use existing node_modules"
    )
    args = parser.parse_args()
    env = {k: v for k, v in os.environ.items() if not k.startswith("VITE_")}
    env.pop("SERVICE_BASE_URL", None)
    npm = shutil.which("npm")
    if not npm:
        parser.error("Node.js/npm is required to build Console release assets")
    # Host's unified build consumes the embedded Context library and its dependencies.
    for name, frontend, mode in (
        ("databridge", ROOT / "packages/qwenpaw-data-context/frontend", "console"),
        ("chat", PACKAGE / "frontend", "production"),
    ):
        if not args.skip_install:
            subprocess.run(
                [npm, "ci", "--no-audit", "--no-fund"],
                cwd=frontend,
                env=env,
                check=True,
            )
        subprocess.run(
            [npm, "run", "build", "--", "--mode", mode],
            cwd=frontend,
            env=env,
            check=True,
        )
        target = STATIC / name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(frontend / "dist", target)
        for document in ("LICENSE", "NOTICE"):
            shutil.copyfile(ROOT / document, target / document)
    print(f"Console assets ready for packaging: {STATIC}")


if __name__ == "__main__":
    main()
