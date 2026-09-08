# -*- coding: utf-8 -*-
"""Artifact 文件路径解析工具。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath


@dataclass(frozen=True)
class ResolvedArtifactPath:
    """Canonical session-relative path and its resolved host path."""

    relative_path: str
    host_path: Path


@dataclass(frozen=True)
class ArtifactPathContext:
    """Map model-visible artifact paths to the host session directory."""

    host_artifact_dir: Path
    model_artifact_dir: Path | str

    def __post_init__(self) -> None:
        host_dir = Path(self.host_artifact_dir).resolve()
        model_dir = str(self.model_artifact_dir).strip()
        model_posix = PurePosixPath(model_dir.replace("\\", "/"))
        model_windows = PureWindowsPath(model_dir)
        if not (model_posix.is_absolute() or model_windows.is_absolute()):
            raise ValueError("model_artifact_dir must be an absolute path")
        if ".." in model_posix.parts or ".." in model_windows.parts:
            raise ValueError("model_artifact_dir must not contain traversal")
        object.__setattr__(self, "host_artifact_dir", host_dir)
        object.__setattr__(self, "model_artifact_dir", model_dir)

    def resolve_ref(self, path: str) -> ResolvedArtifactPath:
        """Validate and resolve a FileRef path within the current session."""
        raw = str(path or "").strip()
        if not raw:
            raise ValueError("artifact path must not be blank")

        candidate = PurePosixPath(raw.replace("\\", "/"))
        windows_candidate = PureWindowsPath(raw)
        if ".." in candidate.parts or ".." in windows_candidate.parts:
            raise ValueError("artifact path must not contain traversal")
        if windows_candidate.is_absolute():
            model_dir = PureWindowsPath(str(self.model_artifact_dir))
            try:
                relative = windows_candidate.relative_to(model_dir)
            except ValueError as exc:
                raise ValueError(
                    "absolute artifact path is outside the current session "
                    "artifacts root"
                ) from exc
            candidate = PurePosixPath(*relative.parts)
        elif candidate.is_absolute():
            model_dir = PurePosixPath(
                str(self.model_artifact_dir).replace("\\", "/"),
            )
            try:
                candidate = candidate.relative_to(model_dir)
            except ValueError as exc:
                raise ValueError(
                    "absolute artifact path is outside the current session "
                    "artifacts root"
                ) from exc
        elif windows_candidate.drive:
            raise ValueError("artifact path must not use a host drive")

        if not candidate.parts or candidate == PurePosixPath("."):
            raise ValueError("artifact path must reference a session file")

        relative_path = candidate.as_posix()
        host_path = (self.host_artifact_dir / relative_path).resolve()
        if not host_path.is_relative_to(self.host_artifact_dir):
            raise ValueError("artifact path escapes the current session directory")
        return ResolvedArtifactPath(
            relative_path=relative_path,
            host_path=host_path,
        )

    def resolve_path(self, path: str) -> Path:
        """Resolve a validated FileRef path to its host absolute path."""
        return self.resolve_ref(path).host_path

    def contains(self, path: Path) -> bool:
        """判断路径是否仍落在 host artifact directory 内。"""
        return Path(path).resolve().is_relative_to(self.host_artifact_dir)
