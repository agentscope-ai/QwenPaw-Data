# -*- coding: utf-8 -*-
"""Env snapshot for tracing. Does not read business-package settings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from qwenpaw_data.tracing.export.install import (
    TracingConfig,
    basic_auth_headers,
    otlp_traces_endpoint,
)

_ENABLED = {"1", "true", "yes", "on"}


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _ENABLED


@dataclass(frozen=True)
class Settings:
    enabled: bool
    debug: bool
    backend: str
    host: str
    public_key: str
    secret_key: str
    service_name: str
    source_release_version: str

    @classmethod
    def from_env(cls) -> Settings:
        """Read TRACING_* and SOURCE_RELEASE_VERSION."""
        return cls(
            enabled=_flag("TRACING_ENABLED"),
            debug=_flag("TRACING_DEBUG"),
            backend=os.environ.get("TRACING_BACKEND", "langfuse").strip() or "langfuse",
            host=os.environ.get("TRACING_HOST", "").strip(),
            public_key=os.environ.get("TRACING_PUBLIC_KEY", "").strip(),
            secret_key=os.environ.get("TRACING_SECRET_KEY", "").strip(),
            service_name=os.environ.get("TRACING_SERVICE_NAME", "").strip(),
            source_release_version=os.environ.get("SOURCE_RELEASE_VERSION", "").strip()
            or "unknown",
        )

    def configured(self) -> bool:
        return self.enabled and bool(self.host and self.public_key and self.secret_key)

    def to_config(
        self,
        *,
        service_name: str = "",
        id_generator: Any = None,
        head_processor: Any = None,
    ) -> TracingConfig:
        name = service_name or self.service_name
        ready = self.configured()
        return TracingConfig(
            service_name=name,
            endpoint=otlp_traces_endpoint(self.host) if ready else None,
            headers=basic_auth_headers(self.backend, self.public_key, self.secret_key)
            if ready
            else {},
            debug=self.debug,
            id_generator=id_generator,
            head_processor=head_processor,
            source_release_version=self.source_release_version,
        )


__all__ = ["Settings"]
