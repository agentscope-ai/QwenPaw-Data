# -*- coding: utf-8 -*-
from __future__ import annotations

from functools import cached_property
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, StringConstraints, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

WorkspaceBackend = Literal["local", "docker", "fc-sandbox", "acs-sandbox"]
WORKSPACE_BACKENDS = ("local", "docker", "fc-sandbox", "acs-sandbox")
CLOUD_SANDBOX_BACKENDS = frozenset({"fc-sandbox", "acs-sandbox"})
DEFAULT_DOCKER_HOST_ALIAS = "host.docker.internal"
# Preinstalled in Docker workspaces so analysis tasks do not pip install per run.
DOCKER_ANALYSIS_STACK = ("pandas", "numpy", "matplotlib", "openpyxl", "tabulate")

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class SandboxSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="QWENPAW_DATA_WORKSPACE_SANDBOX_", extra="ignore"
    )

    idle_ttl_seconds: int = Field(default=900, gt=0)
    timeout_seconds: int = Field(default=900, ge=60)
    renew_interval_seconds: float = Field(default=30, gt=0, le=30)


class WorkspaceOssSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="QWENPAW_DATA_WORKSPACE_OSS_", extra="ignore"
    )

    bucket: str = ""
    prefix: str = ""
    endpoint: str = ""

    def require(self) -> None:
        if not self.bucket:
            raise ValueError("QWENPAW_DATA_WORKSPACE_OSS_BUCKET is required")
        if not self.prefix:
            raise ValueError("QWENPAW_DATA_WORKSPACE_OSS_PREFIX is required")
        if not self.endpoint:
            raise ValueError("QWENPAW_DATA_WORKSPACE_OSS_ENDPOINT is required")


class FcSandboxSettings(BaseSettings):
    """FC sandbox over E2B with a per-user RAM role; VPC is all-or-nothing."""

    model_config = SettingsConfigDict(env_prefix="FC_", extra="ignore")

    api_key: NonEmptyStr = Field(repr=False)
    api_url: NonEmptyStr
    domain: NonEmptyStr
    template_id: NonEmptyStr
    role_arn_pattern: NonEmptyStr
    vpc_id: str = ""
    vswitch_ids: str = ""
    security_group_id: str = ""

    @field_validator("template_id")
    @classmethod
    def _require_prebuilt_template(cls, value: str) -> str:
        if value == "base":
            raise ValueError(
                "FC_TEMPLATE_ID must name a prebuilt QwenPawData template"
            )
        return value

    @field_validator("role_arn_pattern")
    @classmethod
    def _require_uid_placeholder(cls, value: str) -> str:
        if "{uid}" not in value:
            raise ValueError("FC_ROLE_ARN_PATTERN must contain {uid}")
        return value

    def vpc_config(self) -> dict[str, str | list[str]] | None:
        """Parse optional FC runtime networking; partial configuration is invalid."""
        values = {
            "FC_VPC_ID": self.vpc_id.strip(),
            "FC_VSWITCH_IDS": self.vswitch_ids.strip(),
            "FC_SECURITY_GROUP_ID": self.security_group_id.strip(),
        }
        if not any(values.values()):
            return None
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ValueError("FC VPC configuration requires: " + ", ".join(missing))
        vswitch_ids = [value.strip() for value in values["FC_VSWITCH_IDS"].split(",")]
        if not all(vswitch_ids):
            raise ValueError("FC_VSWITCH_IDS must not contain empty entries")
        return {
            "vpcId": values["FC_VPC_ID"],
            "vSwitchIds": vswitch_ids,
            "securityGroupId": values["FC_SECURITY_GROUP_ID"],
        }


class AcsSandboxSettings(BaseSettings):
    """ACS Agent Sandbox over the native E2B protocol; resources are ACS-owned."""

    model_config = SettingsConfigDict(env_prefix="ACS_", extra="ignore")

    api_key: NonEmptyStr = Field(repr=False)
    domain: NonEmptyStr
    template: NonEmptyStr  # SandboxSet name
    oss_pv: NonEmptyStr
    credential_provider: NonEmptyStr
    agent_name: NonEmptyStr


class WorkspaceSettings(BaseSettings):
    """Workspace execution backend and Docker pool settings."""

    model_config = SettingsConfigDict(
        env_prefix="QWENPAW_DATA_WORKSPACE_",
        extra="ignore",
        populate_by_name=True,
    )

    backend: WorkspaceBackend = "local"
    ttl: float = 3600.0
    sweep_interval: float = 300.0
    # The QWENPAW_DATA_DOCKER_* names are the 0.3 spellings.
    docker_base_image: str = Field(
        default="python:3.11-slim",
        validation_alias=AliasChoices(
            "QWENPAW_DATA_WORKSPACE_DOCKER_BASE_IMAGE",
            "QWENPAW_DATA_DOCKER_BASE_IMAGE",
        ),
    )
    docker_node_version: str | None = "20"
    docker_extra_pip: str = Field(
        default="",
        validation_alias=AliasChoices(
            "QWENPAW_DATA_WORKSPACE_DOCKER_EXTRA_PIP",
            "QWENPAW_DATA_DOCKER_EXTRA_PIP",
        ),
    )
    docker_gateway_port: int = 5600
    # Hostname the container uses to reach Host-side loopback services.
    docker_host_alias: str = Field(
        default=DEFAULT_DOCKER_HOST_ALIAS,
        validation_alias=AliasChoices(
            "QWENPAW_DATA_WORKSPACE_DOCKER_HOST_ALIAS",
            "QWENPAW_DATA_DOCKER_HOST_ALIAS",
        ),
    )

    @cached_property
    def sandbox(self) -> SandboxSettings:
        return SandboxSettings()

    @cached_property
    def oss(self) -> WorkspaceOssSettings:
        return WorkspaceOssSettings()

    @cached_property
    def fc(self) -> FcSandboxSettings:
        return FcSandboxSettings()

    @cached_property
    def acs(self) -> AcsSandboxSettings:
        return AcsSandboxSettings()

    @property
    def is_cloud_sandbox(self) -> bool:
        return self.backend in CLOUD_SANDBOX_BACKENDS

    @field_validator("backend", mode="before")
    @classmethod
    def _normalize_backend(cls, value: object) -> str:
        text = str(value).strip().lower()
        if text not in WORKSPACE_BACKENDS:
            raise ValueError(
                "QWENPAW_DATA_WORKSPACE_BACKEND must be one of "
                f"{', '.join(WORKSPACE_BACKENDS)}, got {value!r}",
            )
        return text

    def extra_pip_packages(self) -> list[str]:
        """Docker requirements: analysis stack, host-pinned runtime, env extras.

        The image installs agentscope with ``--no-deps`` and leaves mcp
        unpinned, so the full agentscope tree and the host's mcp version are
        installed up front to keep the in-container gateway compatible.
        """
        packages = list(DOCKER_ANALYSIS_STACK)
        for dist in ("agentscope", "mcp"):
            try:
                packages.append(f"{dist}=={_dist_version(dist)}")
            except PackageNotFoundError:
                continue
        for part in self.docker_extra_pip.split(","):
            name = part.strip()
            if name and name not in packages:
                packages.append(name)
        return packages

    def docker_loopback_alias(self) -> str:
        return self.docker_host_alias.strip() or DEFAULT_DOCKER_HOST_ALIAS
