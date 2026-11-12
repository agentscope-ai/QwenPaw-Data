"""Execute bounded GoogleSQL queries through the BigQuery SDK."""

import json

from ..config import BigQueryConfig


def connect(config: BigQueryConfig):
    """Create a client with explicit service-account and billing configuration."""
    from google.cloud import bigquery
    from google.oauth2 import service_account

    credentials = service_account.Credentials.from_service_account_info(
        json.loads(config.credentials_json)
    )
    return bigquery.Client(
        project=config.project,
        credentials=credentials,
        location=config.location,
    )


def query(client, config: BigQueryConfig, sql: str, *, max_rows: int):
    """Apply the billing limit and bound row downloads from the completed job."""
    from google.cloud import bigquery

    job_config = bigquery.QueryJobConfig(
        use_legacy_sql=False,
        maximum_bytes_billed=config.maximum_bytes_billed,
    )
    job = client.query(sql, job_config=job_config)
    result = job.result(max_results=max_rows + 1)
    description = tuple((field.name,) for field in result.schema)
    rows = tuple(tuple(row) for row in result)
    return description, rows, result.total_rows


def probe(client) -> None:
    """Check dataset-list access without submitting a billed query."""
    tuple(client.list_datasets(max_results=10))


def execute(client, config: BigQueryConfig, sql: str, *, max_rows: int):
    """Complete one write job and preserve its affected-row count when reported."""
    from google.cloud import bigquery

    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            use_legacy_sql=False, maximum_bytes_billed=config.maximum_bytes_billed
        ),
    )
    result = job.result(max_results=max_rows + 1)
    description = tuple((field.name,) for field in result.schema)
    rows = tuple(tuple(row) for row in result)
    count = job.num_dml_affected_rows
    return description, rows, count if count is not None else -1
