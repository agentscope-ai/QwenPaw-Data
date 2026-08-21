#!/usr/bin/env python3
"""
event_evidence_scorer.py

Score and rank candidate causal events against an anomaly anchor.

Inputs
------
--anomaly-file : CSV with one row describing the anomaly anchor.
    Required columns: 指标名, 异常开始日期, 异常结束日期, 变动方向, 变动幅度
    Optional columns: 关键维度

--events-file  : CSV with extracted business events (one row per event).
    Required columns: 事件名称, 开始日期, 结束日期, 预期方向, 影响维度, 来源类型, 原文摘要

Output
------
Ranked events with four sub-scores and a final weighted score.

Scoring formula
---------------
  final = 0.4 * time_overlap
        + 0.3 * direction_consistency
        + 0.2 * dimension_match
        + 0.1 * source_reliability

Confidence levels
-----------------
  ≥ 0.7  →  高
  0.4–0.7 →  中
  < 0.4  →  低
"""

import argparse
import csv
import sys
from datetime import datetime, timedelta


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WEIGHTS = {"time": 0.4, "direction": 0.3, "dimension": 0.2, "source": 0.1}

SOURCE_RELIABILITY = {
    "结构化文档": 1.0,
    "周报": 0.8,
    "对话输入": 0.5,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_date(raw: str):
    """Return a date object, or None if the string is empty / unparseable."""
    if not raw:
        return None
    s = raw.strip().replace("[推测]", "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def time_overlap_score(
    event_start, event_end, anomaly_start, anomaly_end
) -> float:
    """
    overlap_days / anomaly_window_days.
    Returns 0 when the event has no usable date info.
    """
    if event_start is None and event_end is None:
        return 0.0

    es = event_start or event_end
    ee = event_end or event_start

    overlap_start = max(es, anomaly_start)
    overlap_end = min(ee, anomaly_end)

    anomaly_days = max(1, (anomaly_end - anomaly_start).days + 1)
    overlap_days = max(0, (overlap_end - overlap_start).days + 1)
    return min(1.0, overlap_days / anomaly_days)


def direction_score(event_direction: str, anomaly_direction: str) -> float:
    """
    Measure whether the event's expected impact is consistent with the
    observed anomaly direction.

    Scoring table:
      anomaly=下降, event=负向 → 1.0  (event lowers metric, matches drop)
      anomaly=上升, event=正向 → 1.0  (event raises metric, matches rise)
      双向 / 不确定            → 0.5  (partial or unknown alignment)
      opposite direction        → 0.0
    """
    ed = (event_direction or "不确定").strip()
    ad = (anomaly_direction or "").strip()

    if ed in ("不确定", ""):
        return 0.5
    if ed == "双向":
        return 0.5
    if ad == "下降":
        return 1.0 if ed == "负向" else 0.0
    if ad == "上升":
        return 1.0 if ed == "正向" else 0.0
    # anomaly direction is "波动" or unspecified
    return 0.5


def dimension_score(event_dimension: str, key_dimension: str) -> float:
    """
    Whether the event's affected dimension overlaps with the key anomaly dimension.
    Returns 0.5 when either side is unspecified (neutral, not penalised).
    """
    ed = (event_dimension or "").strip()
    kd = (key_dimension or "").strip()

    if not kd:
        return 0.5  # no key dimension to match against
    if not ed:
        return 0.5  # event dimension unknown
    if ed == kd or ed in kd or kd in ed:
        return 1.0
    return 0.0


def source_reliability_score(source_type: str) -> float:
    return SOURCE_RELIABILITY.get((source_type or "").strip(), 0.5)


def confidence_level(score: float) -> str:
    if score >= 0.7:
        return "高"
    if score >= 0.4:
        return "中"
    return "低"


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def read_csv(path: str):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_csv(path: str, rows: list, fieldnames: list):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Score candidate causal events against an anomaly anchor."
    )
    parser.add_argument("--anomaly-file", required=True, help="Anomaly anchor CSV (step 1 output)")
    parser.add_argument("--events-file", required=True, help="Extracted events CSV (step 3 output)")
    parser.add_argument("--output-file", default=None, help="Output CSV path (optional)")
    args = parser.parse_args()

    # --- Load anomaly anchor ---
    anchors = read_csv(args.anomaly_file)
    if not anchors:
        print("ERROR: anomaly anchor file is empty.", file=sys.stderr)
        sys.exit(1)
    anchor = anchors[0]

    anomaly_start = parse_date(anchor.get("异常开始日期", ""))
    anomaly_end = parse_date(anchor.get("异常结束日期", ""))
    if anomaly_start is None or anomaly_end is None:
        print(
            "ERROR: anomaly anchor must contain valid 异常开始日期 and 异常结束日期.",
            file=sys.stderr,
        )
        sys.exit(1)

    anomaly_direction = anchor.get("变动方向", "").strip()
    key_dimension = anchor.get("关键维度", "").strip()
    metric_name = anchor.get("指标名", "").strip()
    magnitude = anchor.get("变动幅度", "").strip()

    # --- Load events ---
    events = read_csv(args.events_file)
    if not events:
        print("ERROR: events file is empty.", file=sys.stderr)
        sys.exit(1)

    # --- Score each event ---
    results = []
    for ev in events:
        event_start = parse_date(ev.get("开始日期", ""))
        event_end = parse_date(ev.get("结束日期", ""))
        event_direction = ev.get("预期方向", "不确定")
        event_dimension = ev.get("影响维度", "")
        source_type = ev.get("来源类型", "对话输入")

        t = time_overlap_score(event_start, event_end, anomaly_start, anomaly_end)
        d = direction_score(event_direction, anomaly_direction)
        dim = dimension_score(event_dimension, key_dimension)
        s = source_reliability_score(source_type)

        final = (
            WEIGHTS["time"] * t
            + WEIGHTS["direction"] * d
            + WEIGHTS["dimension"] * dim
            + WEIGHTS["source"] * s
        )

        results.append(
            {
                "事件名称": ev.get("事件名称", ""),
                "可信度": confidence_level(final),
                "综合评分": round(final, 2),
                "时间覆盖度": round(t, 2),
                "方向一致性": round(d, 2),
                "维度吻合度": round(dim, 2),
                "来源可信度": round(s, 2),
                "预期方向": event_direction,
                "影响维度": event_dimension,
                "来源类型": source_type,
                "原文摘要": ev.get("原文摘要", ""),
            }
        )

    results.sort(key=lambda x: x["综合评分"], reverse=True)

    # --- Print to stdout ---
    header = (
        f"\n异常窗口: {anomaly_start} ~ {anomaly_end}"
        f"  |  指标: {metric_name}  |  方向: {anomaly_direction}  |  幅度: {magnitude}"
        f"  |  关键维度: {key_dimension or '未指定'}\n"
    )
    print(header)

    col_w = [20, 6, 8, 8, 8, 8, 8]
    cols = ["事件名称", "可信度", "综合评分", "时间覆盖度", "方向一致性", "维度吻合度", "来源可信度"]
    header_row = "  ".join(str(c).ljust(w) for c, w in zip(cols, col_w)) + "  原文摘要"
    print(header_row)
    print("-" * 110)

    for r in results:
        summary = r["原文摘要"]
        if len(summary) > 35:
            summary = summary[:35] + "..."
        row = "  ".join(
            str(r[c]).ljust(w) for c, w in zip(cols, col_w)
        ) + f"  {summary}"
        print(row)

    # --- Write output file ---
    if args.output_file:
        fieldnames = [
            "事件名称", "可信度", "综合评分",
            "时间覆盖度", "方向一致性", "维度吻合度", "来源可信度",
            "预期方向", "影响维度", "来源类型", "原文摘要",
        ]
        write_csv(args.output_file, results, fieldnames)
        print(f"\n结果已写入: {args.output_file}")


if __name__ == "__main__":
    main()
