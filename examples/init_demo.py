#!/usr/bin/env python3
"""Create and verify the portable QwenPaw Data GAAP demo dataset."""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
import urllib.error
import urllib.request
import uuid
from datetime import date, timedelta
from pathlib import Path


EXAMPLES = Path(__file__).resolve().parent
REPO_ROOT = EXAMPLES.parent
DEMO_DATASOURCE_ID = "postgresql-demo-gaap"
DEMO_DATASOURCE_NAME = "Demo PG - GAAP use case"
EXPECTED_ROW_COUNT = 475
EXPECTED_DAILY_AVERAGES = {
    "2026-03-01": 4.64,
    "2026-03-02": 4.04,
    "2026-03-03": 3.37,
    "2026-03-04": 5.31,
    "2026-03-05": 5.06,
    "2026-03-06": 4.26,
    "2026-03-07": 3.94,
    "2026-03-08": 4.92,
    "2026-03-09": 4.76,
    "2026-03-10": 45.89,
    "2026-03-11": 4.64,
    "2026-03-12": 3.32,
    "2026-03-13": 5.16,
    "2026-03-14": 4.32,
    "2026-03-15": 3.62,
}


def load_repo_environment(path: Path | None = None) -> None:
    """Load the root dotenv without overriding the invoking terminal."""
    configured = os.getenv("QWENPAW_DATA_ENV_FILE", "").strip()
    env_path = (
        path
        if path is not None
        else Path(configured).expanduser()
        if configured
        else REPO_ROOT / ".env"
    )
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not key.replace("_", "a").isalnum() or key[0].isdigit():
            raise ValueError(f"Invalid environment key in {env_path}: {key!r}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def demo_rows() -> list[tuple[str, str, str, str, str, float, float]]:
    """Return the fictional GAAP rows shipped with the public demo bundle."""
    rng = random.Random(42)
    rows: list[tuple[str, str, str, str, str, float, float]] = []
    start = date(2026, 3, 1)
    products = ["X", "Y"]
    regions = ["North", "South", "East", "West"]
    user_types = ["student", "enterprise", "individual"]
    user_seq = 0

    for day_offset in range(15):
        ds = start + timedelta(days=day_offset)
        ds_value = ds.isoformat()
        for _ in range(30):
            user_seq += 1
            rows.append(
                (
                    ds_value,
                    rng.choice(products),
                    rng.choice(regions),
                    rng.choice(user_types),
                    f"u{user_seq:05d}",
                    round(rng.uniform(1.0, 8.0), 2),
                    round(rng.uniform(0.0, 25.0), 2),
                ),
            )

        if ds == date(2026, 3, 10):
            for _ in range(15):
                user_seq += 1
                rows.append(
                    (
                        ds_value,
                        "X",
                        rng.choice(regions),
                        "student",
                        f"u{user_seq:05d}",
                        round(rng.uniform(40.0, 60.0), 2),
                        round(rng.uniform(15.0, 30.0), 2),
                    ),
                )
            for _ in range(10):
                user_seq += 1
                rows.append(
                    (
                        ds_value,
                        "X",
                        "North",
                        "enterprise",
                        f"u{user_seq:05d}",
                        round(rng.uniform(70.0, 100.0), 2),
                        round(rng.uniform(20.0, 40.0), 2),
                    ),
                )
    return rows


def _validate_demo(count: int, averages: dict[str, float], backend: str) -> None:
    if count != EXPECTED_ROW_COUNT:
        raise RuntimeError(f"{backend} demo row count failed: {count}")
    if averages != EXPECTED_DAILY_AVERAGES:
        raise RuntimeError(f"{backend} demo verification failed: {averages!r}")


def seed_sqlite(path: Path) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    sql = (EXAMPLES / "demo" / "sqlite" / "init.sql").read_text(encoding="utf-8")
    with sqlite3.connect(path) as connection:
        connection.executescript(sql)
        connection.executemany(
            "INSERT INTO dws_gaap_di "
            "(ds, product, region, user_type, user_id, gaap_val, ytd_gaap) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            demo_rows(),
        )
        count = int(
            connection.execute("SELECT COUNT(*) FROM dws_gaap_di").fetchone()[0]
        )
    actual = query_sqlite(path)
    _validate_demo(count, actual, "SQLite")
    print(f"SQLite demo ready: {path}")


def query_sqlite(path: Path) -> dict[str, float]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT ds, ROUND(AVG(gaap_val), 2) "
            "FROM dws_gaap_di "
            "WHERE product = 'X' AND ytd_gaap >= 10 "
            "GROUP BY ds ORDER BY ds",
        ).fetchall()
    return {str(ds): float(value) for ds, value in rows}


def seed_postgres(dsn: str) -> None:
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            "PostgreSQL seeding requires psycopg; run with `uv run python`."
        ) from exc
    sql = (EXAMPLES / "demo" / "postgres" / "init.sql").read_text(encoding="utf-8")
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            cursor.executemany(
                "INSERT INTO dws_gaap_di "
                "(ds, product, region, user_type, user_id, gaap_val, ytd_gaap) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                demo_rows(),
            )
            cursor.execute("SELECT COUNT(*) FROM dws_gaap_di")
            count = int(cursor.fetchone()[0])
            cursor.execute(
                "SELECT ds, ROUND(AVG(gaap_val), 2) "
                "FROM dws_gaap_di "
                "WHERE product = 'X' AND ytd_gaap >= 10 "
                "GROUP BY ds ORDER BY ds",
            )
            rows = cursor.fetchall()
    actual = {str(ds): float(value) for ds, value in rows}
    _validate_demo(count, actual, "PostgreSQL")
    print("PostgreSQL demo seeded and verified.")


def configure_demo_bundle(
    base_url: str,
    port: int,
    *,
    host: str = "127.0.0.1",
    dbname: str = "qwenpaw_data_demo",
    user: str = "qwenpaw_data",
    password: str = "qwenpaw-data-demo",
) -> str:
    """Import the bundled semantic workbook and attach local PG credentials."""
    base_url = base_url.rstrip("/")
    headers = _auth_headers()
    workbook = EXAMPLES / "demo_semantic_config.xlsx"
    imported = _request_file(
        f"{base_url}/api/semantic-config/import/excel",
        workbook,
        headers=headers,
    )
    if not isinstance(imported, dict) or not imported.get("success"):
        raise RuntimeError(f"semantic demo import failed: {imported!r}")

    configured = _request_json(
        f"{base_url}/api/semantic-config/datasource/{DEMO_DATASOURCE_ID}",
        method="PUT",
        headers={**headers, "Content-Type": "application/json"},
        payload={
            "datasource_name": DEMO_DATASOURCE_NAME,
            "datasource_type": "postgresql",
            "config": {
                "host": host,
                "port": port,
                "dbname": dbname,
                "user": user,
                "password": password,
            },
        },
    )
    if (
        not isinstance(configured, dict)
        or configured.get("datasource_id") != DEMO_DATASOURCE_ID
    ):
        raise RuntimeError(f"demo datasource configuration failed: {configured!r}")
    print(
        "Imported semantic demo configuration:",
        json.dumps(imported.get("summary", {}), sort_keys=True),
    )
    print(f"Configured DataBridge datasource: {DEMO_DATASOURCE_ID}")
    return DEMO_DATASOURCE_ID


def _auth_headers() -> dict[str, str]:
    token = (
        os.getenv("QWENPAW_DATA_CLIENT_API_TOKEN") or os.getenv("QWENPAW_DATA_API_TOKEN") or ""
    ).strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _request_file(url: str, path: Path, *, headers: dict[str, str]) -> object:
    boundary = f"----QwenPawDataDemo{uuid.uuid4().hex}"
    content = path.read_bytes()
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            (
                'Content-Disposition: form-data; name="file"; '
                f'filename="{path.name}"\r\n'
            ).encode(),
            b"Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n",
            content,
            f"\r\n--{boundary}--\r\n".encode(),
        ],
    )
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            **headers,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"DataBridge returned HTTP {exc.code}: {detail}") from exc


def _request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, object] | None = None,
) -> object:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=body, headers=headers or {}, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"DataBridge returned HTTP {exc.code}: {detail}") from exc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sqlite-path",
        type=Path,
        default=EXAMPLES / "demo" / "data" / "qwenpaw-data-demo.sqlite",
    )
    parser.add_argument("--postgres-dsn", help="Also seed and verify PostgreSQL")
    registration = parser.add_mutually_exclusive_group()
    registration.add_argument(
        "--register",
        action="store_true",
        help="Register with QWENPAW_DATA_CM_BASE_URL or the local default",
    )
    registration.add_argument(
        "--register-url",
        help="Import the semantic demo and configure its PostgreSQL datasource",
    )
    parser.add_argument(
        "--postgres-port",
        type=int,
        default=int(os.getenv("QWENPAW_DATA_DEMO_POSTGRES_PORT", "55432")),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_repo_environment()
    args = parse_args(argv)
    try:
        seed_sqlite(args.sqlite_path)
        if args.postgres_dsn:
            seed_postgres(args.postgres_dsn)
        register_url = args.register_url
        if args.register:
            register_url = os.getenv(
                "QWENPAW_DATA_CM_BASE_URL",
                "http://127.0.0.1:8765",
            )
        if register_url:
            configure_demo_bundle(register_url, args.postgres_port)
    except (OSError, RuntimeError, sqlite3.Error, urllib.error.URLError) as exc:
        print(f"demo initialization failed: {exc}", file=sys.stderr)
        return 1
    print(
        "Expected March 10 valid-user GAAP spike:",
        EXPECTED_DAILY_AVERAGES["2026-03-10"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
