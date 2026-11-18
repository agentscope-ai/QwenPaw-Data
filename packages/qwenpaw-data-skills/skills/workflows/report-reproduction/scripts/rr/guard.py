# -*- coding: utf-8 -*-
"""One chat advances one Section or one period. Enforces R4."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rr.common import (
    PRODUCT_DIR,
    ReportCliError,
    _SAFE_ID_RE,
    utc_now,
    validated_section_id,
    write_text_atomic,
)


class SectionTurnGuard:
    """Persist the report-reproduction phase assigned to one chat.

    One chat may advance exactly one baseline Section, or exactly one target
    period. That constraint is what makes per-Section human review possible, so
    it survives the move out of the host unchanged.
    """

    def __init__(
        self,
        artifact_dir: Path,
        *,
        chat_id: str,
        turn_root: Path | None = None,
    ) -> None:
        # Cron mode records the chat under the workspace, never the reference.
        self._product_root = (
            Path(turn_root)
            if turn_root is not None
            else Path(artifact_dir) / PRODUCT_DIR
        )
        self._chat_id = chat_id.strip()

    @property
    def enabled(self) -> bool:
        return bool(self._chat_id)

    def mark_planning(self) -> None:
        """Reserve this chat for report parsing, clarification, and planning."""
        if not self.enabled:
            return
        payload = self._load()
        mode = self._mode(payload)
        if mode and mode != "planning":
            raise ReportCliError("报告解析与规划必须在单独的 chat 中进行")
        self._write({"mode": "planning"})

    def start_target_run(self, run_period: str) -> None:
        """Allow one target-period chat to advance every run Section."""
        normalized_period = self.ensure_target_run_allowed(run_period)
        if not self.enabled:
            return
        self._write({"mode": "target_run", "run_period": normalized_period})

    def ensure_target_run_allowed(self, run_period: str) -> str:
        """Validate target-period chat scope without changing persisted state."""
        normalized_period = (run_period or "").strip()
        if not self.enabled:
            return normalized_period
        if not normalized_period:
            raise ReportCliError("缺少 --run-period：目标周期执行必须指定周期")
        payload = self._load()
        mode = self._mode(payload)
        claimed_period = str(payload.get("run_period") or "").strip()
        if mode == "planning":
            raise ReportCliError(
                "目标周期执行必须在报告规划完成之后、于单独的 chat 中进行"
            )
        if mode == "baseline_section":
            raise ReportCliError(
                "目标周期执行必须在基线 Section 完成之后、于单独的 chat 中进行"
            )
        if mode == "target_run" and claimed_period != normalized_period:
            raise ReportCliError(
                "一个 chat 只能执行一个目标周期；"
                f"当前 chat 已绑定周期 {claimed_period!r}"
            )
        return normalized_period

    def claim(self, section_id: str, *, run_period: str | None = None) -> None:
        if not self.enabled:
            return
        normalized = validated_section_id(section_id)
        if run_period is not None:
            self.start_target_run(run_period)
            return

        payload = self._load()
        mode = self._mode(payload)
        if mode == "planning":
            raise ReportCliError(
                "基线 Section 执行必须在报告解析与规划完成之后、于单独的 chat 中进行"
            )
        if mode == "target_run":
            raise ReportCliError("基线 Section 执行不能在目标周期 chat 中进行")
        claimed = str(payload.get("section_id") or "").strip()
        if mode == "baseline_section" and claimed and claimed != normalized:
            raise ReportCliError(
                f"一个 chat 只能推进一个 Section；当前 chat 已绑定 Section {claimed!r}"
            )
        self._write({"mode": "baseline_section", "section_id": normalized})

    def claimed_section(self) -> str | None:
        if not self.enabled:
            return None
        claimed = str(self._load().get("section_id") or "").strip()
        return claimed or None

    def ensure_no_section_claimed(self, action: str) -> None:
        if not self.enabled:
            return
        payload = self._load()
        mode = self._mode(payload)
        if mode == "planning":
            raise ReportCliError(
                f"{action} 必须在报告规划完成之后、于单独的 chat 中进行"
            )
        if mode == "target_run":
            raise ReportCliError(f"{action} 必须在目标周期执行开始之前完成")
        claimed = str(payload.get("section_id") or "").strip()
        if mode == "baseline_section" and claimed:
            raise ReportCliError(
                f"{action} 必须在 Section {claimed!r} 完成之后、于单独的 chat 中进行"
            )

    def ensure_compile_allowed(
        self,
        action: str,
        *,
        run_period: str | None = None,
    ) -> None:
        if run_period is None:
            self.ensure_no_section_claimed(action)
            return
        if not self.enabled:
            return
        normalized_period = run_period.strip()
        payload = self._load()
        mode = self._mode(payload)
        claimed_period = str(payload.get("run_period") or "").strip()
        if mode != "target_run" or claimed_period != normalized_period:
            raise ReportCliError(
                f"{action} 需要先在当前 chat 中执行 prepare-run --period "
                f"{normalized_period!r}"
            )

    def _load(self) -> dict[str, Any]:
        path = self._path()
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _mode(payload: dict[str, Any]) -> str:
        mode = str(payload.get("mode") or "").strip()
        if not mode and payload.get("section_id"):
            return "baseline_section"
        return mode

    def _write(self, values: dict[str, Any]) -> None:
        path = self._path()
        write_text_atomic(
            path,
            json.dumps(
                {
                    "chat_id": self._chat_id,
                    **values,
                    "updated_at": utc_now(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )

    def _path(self) -> Path:
        safe_chat_id = _SAFE_ID_RE.sub("_", self._chat_id).strip("._-")
        if not safe_chat_id:
            raise ReportCliError("--chat-id 非法，无法作为 turn 记录文件名")
        return self._product_root / "turns" / f"{safe_chat_id}.json"
