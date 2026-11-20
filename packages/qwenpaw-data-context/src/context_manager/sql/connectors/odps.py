"""Share PyODPS execution across public AccessKey and STS credentials."""

from itertools import islice

from ..config import OdpsConfig


def connect(config: OdpsConfig):
    """Create a request-owned client with the selected credential source."""
    from odps import ODPS
    from odps.accounts import AliyunAccount, StsAccount

    if config.sts_token:
        account = StsAccount(
            config.access_key_id, config.access_key_secret, config.sts_token
        )
    else:
        account = AliyunAccount(config.access_key_id, config.access_key_secret)
    return ODPS(account=account, project=config.project, endpoint=config.endpoint)


def query(client, sql: str, *, max_rows: int):
    """Wait for one query through the SDK and detach a bounded result."""
    instance = client.run_sql(sql)
    instance.wait_for_success()
    with instance.open_reader() as reader:
        schema = getattr(reader, "schema", None) or getattr(reader, "_schema", None)
        columns = (
            tuple(column.name for column in schema.columns)
            if schema is not None
            else ()
        )
        rows = []
        for record in islice(reader, max_rows + 1):
            if not columns:
                columns = tuple(column.name for column in record._columns)
            rows.append(tuple(record.values))
        return (
            tuple((name,) for name in columns),
            tuple(rows),
            getattr(reader, "count", None),
        )


def probe(client, config: OdpsConfig) -> None:
    """Check project access and table-list permission without submitting SQL."""
    if not client.exist_project(config.project):
        raise ValueError("ODPS project is unavailable")
    tuple(islice(client.list_tables(), 10))


def execute(client, sql: str):
    """Wait for a DDL/DML job without treating its status text as table rows."""
    instance = client.run_sql(sql)
    instance.wait_for_success()
    return (), (), -1
