"""Record and verify the local launch target without storing database credentials."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit


def _url_target(value: str) -> tuple:
    url = urlsplit(value)
    host = (
        "localhost"
        if url.hostname in {"localhost", "127.0.0.1", "::1"}
        else url.hostname
    )
    return url.scheme, host, url.port, url.path.rstrip("/"), url.query


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("write", "check"))
    parser.add_argument("--service", choices=("all", "cm", "host"), default="all")
    parser.add_argument("--pid", type=int, action="append", default=[])
    parser.add_argument("--auth-url")
    parser.add_argument("--context-url")
    parser.add_argument("--host-url")
    args = parser.parse_args()
    env_file = Path(os.environ["QWENPAW_DATA_ENV_FILE"]).resolve()
    path = env_file.with_name(env_file.name + ".services.json")
    if args.action == "write" and args.service != "all":
        path = env_file.with_name(env_file.name + f".{args.service}.services.json")
    fingerprint = hashlib.sha256(
        os.environ.get("QWENPAW_DATA_DB_URL", "").encode()
    ).hexdigest()
    if args.action == "write":
        payload = {
            "database": fingerprint,
            "graph_backend": os.environ.get("GRAPH_BACKEND", "neo4j"),
            "pids": args.pid,
            "auth_url": args.auth_url,
            "context_url": args.context_url,
            "host_url": args.host_url,
        }
        path.write_text(json.dumps(payload, indent=2) + "\n")
        return
    paths = [
        path,
        *(
            env_file.with_name(env_file.name + f".{service}.services.json")
            for service in ("cm", "host")
        ),
    ]
    for path in paths:
        if path.exists():
            check_manifest(parser, args, json.loads(path.read_text()), fingerprint)


def check_manifest(parser, args, payload, fingerprint):
    if payload.get(
        "graph_backend", os.environ.get("GRAPH_BACKEND", "neo4j")
    ) != os.environ.get("GRAPH_BACKEND", "neo4j"):
        parser.exit(
            1, "Graph backend differs from running services; restart before setup.\n"
        )
    if payload["database"] != fingerprint:
        parser.exit(
            1,
            "Control database differs from the running service manifest; restart with the intended dotenv.\n",
        )
    for key in ("auth_url", "context_url", "host_url"):
        expected = getattr(args, key)
        if (
            expected
            and payload.get(key)
            and _url_target(payload[key]) != _url_target(expected)
        ):
            parser.exit(1, f"{key} differs from the running service manifest.\n")
    for pid in payload["pids"]:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            parser.exit(
                1, "Service manifest is stale; restart services before setup.\n"
            )


if __name__ == "__main__":
    main()
