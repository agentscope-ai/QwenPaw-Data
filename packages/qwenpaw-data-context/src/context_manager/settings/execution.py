"""Budgets for a Runtime's execution resources."""

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class PoolSettings(BaseModel):
    """Per-executor limits for work units, worker threads and queue waits.

    A work unit is one function call submitted through ``run()``, including
    its synchronous steps and asynchronous waits until the call finishes.

    ``workers`` sets two separate caps to the same value:
    - Admitted work units that have not finished, including async waits.
    - Worker threads concurrently executing this executor's synchronous steps.

    A work unit awaiting an async result holds admission capacity but no worker
    thread. ``max_waiters`` limits work units still waiting for admission.
    """

    workers: int = Field(gt=0)
    max_waiters: int = Field(default=64, ge=0)  # Queued work units.
    queue_timeout_seconds: float = Field(
        default=30, gt=0, allow_inf_nan=False
    )  # Maximum admission wait in seconds.


class ExecutionSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CM_EXECUTION_",
        env_nested_delimiter="__",
        extra="ignore",
        nested_model_default_partial_update=True,
    )

    graph_read: PoolSettings = PoolSettings(
        workers=8
    )  # Synchronous foreground graph reads; shared across this Runtime's Workspaces.
    background: PoolSettings = PoolSettings(
        workers=2, queue_timeout_seconds=120
    )  # Background work units, including async waits and sync steps; shared across Workspaces.
    sql: PoolSettings = PoolSettings(
        workers=4, max_waiters=16, queue_timeout_seconds=5
    )  # User SQL thread and queue budgets, shared across Workspaces.
