# -*- coding: utf-8 -*-
"""Artifact 文件路径解析工具。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ArtifactPathContext:
    """把 artifact 的沙箱视角路径解析到宿主机路径。"""

    base_dir: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_dir", Path(self.base_dir).resolve())

    def resolve_path(self, path: str) -> Path:
        """解析 FileRef/ArtifactItem.path 到宿主机绝对路径。"""
        fp = str(path or "").strip()
        if not fp:
            return self.base_dir
        candidate = Path(fp)
        if candidate.is_absolute() and not (
            fp == "/workspace" or fp.startswith("/workspace/")
        ):
            return candidate.resolve()
        if fp == "/workspace":
            return self.base_dir
        if fp.startswith("/workspace/"):
            fp = fp[len("/workspace/"):]
        return (self.base_dir / fp.lstrip("/")).resolve()

    def contains(self, path: Path) -> bool:
        """判断路径是否仍落在 base_dir 内。"""
        return Path(path).resolve().is_relative_to(self.base_dir)
