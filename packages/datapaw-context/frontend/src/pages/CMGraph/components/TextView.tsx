import { useMemo, useState } from "react";
import { Slider } from "@/design";
import { useTranslation } from "react-i18next";
import type { QueryFrame } from "../types";
import styles from "../index.module.less";

/** Convert a record to a flat text representation.
 *  Handles path-like records (with start/end/segments) and
 *  falls back to JSON.stringify for scalar or other record types. */
function recordToText(record: Record<string, unknown>): string {
  // Path-like record: has start/end/segments (Neo4j path object)
  const start = record.start as { labels?: string[]; properties?: Record<string, unknown> } | undefined;
  const end = record.end as { labels?: string[]; properties?: Record<string, unknown> } | undefined;
  const segments = record.segments as { relationship?: { type?: string } }[] | undefined;

  if (!start || !end) {
    // For non-path records, format each key-value pair
    const entries = Object.entries(record);
    if (entries.length === 0) return "";
    return entries.map(([k, v]) => {
      if (v === null) return `${k}: null`;
      if (typeof v === "string") return `${k}: "${v}"`;
      if (typeof v === "number" || typeof v === "boolean") return `${k}: ${v}`;
      return `${k}: ${JSON.stringify(v)}`;
    }).join(", ");
  }

  const formatProps = (props: Record<string, unknown>) => {
    return Object.entries(props)
      .map(([k, v]) => {
        if (typeof v === "string") return `${k}: "${v}"`;
        if (Array.isArray(v)) return `${k}: ${JSON.stringify(v)}`;
        return `${k}: ${v}`;
      })
      .join(",");
  };

  const startLabel = start.labels?.[0] || "";
  const endLabel = end.labels?.[0] || "";
  const relType = segments?.[0]?.relationship?.type || "";
  const startProps = formatProps((start.properties as Record<string, unknown>) ?? {});
  const endProps = formatProps((end.properties as Record<string, unknown>) ?? {});

  return `(:${startLabel} {${startProps}})-[:${relType}]->(:${endLabel} {${endProps}})`;
}

/** Wrap text at maxWidth, splitting into lines */
function wrapText(text: string, width: number): string[] {
  const lines: string[] = [];
  let remaining = text;
  while (remaining.length > width) {
    lines.push(remaining.slice(0, width));
    remaining = remaining.slice(width);
  }
  if (remaining) lines.push(remaining);
  return lines;
}

interface TextViewProps {
  frame: QueryFrame;
}

export default function TextView({ frame }: TextViewProps) {
  const { t } = useTranslation();
  const [maxWidth, setMaxWidth] = useState(80);

  // Text view uses ONLY rows from the response.
  // Each row is formatted as a text line.
  const records = useMemo(() => {
    return (frame.rows ?? []).map((r) => recordToText(r as Record<string, unknown>));
  }, [frame.rows]);

  if (records.length === 0) {
    return (
      <div className={styles.textViewEmpty}>
        {t("kgBrowser.noRecords")}
      </div>
    );
  }

  // Build the table border
  const colWidth = maxWidth;
  const topBorder = "╔" + "═".repeat(colWidth) + "╗";
  const headerSep = "╠" + "═".repeat(colWidth) + "╣";
  const rowSep = "╟" + "─".repeat(colWidth) + "╢";
  const bottomBorder = "╚" + "═".repeat(colWidth) + "╝";

  // Use actual column names from the response, joined as the header
  const colName = frame.columns?.length ? frame.columns.join(", ") : "p";
  const headerText = colName.slice(0, colWidth).padStart(Math.floor((colWidth + colName.length) / 2)).padEnd(colWidth);

  return (
    <div className={styles.textView}>
      {/* ASCII table content */}
      <div className={styles.textViewContent}>
        <pre className={styles.textViewPre}>
          {topBorder + "\n"}
          {"║" + headerText + "║\n"}
          {headerSep + "\n"}
          {records.map((rec, idx) => {
            const wrapped = wrapText(rec, colWidth);
            const lines = wrapped
              .map((line) => "║" + line.padEnd(colWidth) + "║")
              .join("\n");
            const sep = idx < records.length - 1 ? "\n" + rowSep + "\n" : "\n";
            return lines + sep;
          })}
          {bottomBorder}
        </pre>
      </div>

      {/* Bottom slider bar */}
      <div className={styles.textViewSliderBar}>
        <span className={styles.textViewSliderLabel}>
          {t("kgBrowser.maxColumnWidth")}:
        </span>
        <Slider
          className={styles.textViewSlider}
          min={40}
          max={200}
          value={maxWidth}
          onChange={(val) => setMaxWidth(val)}
        />
      </div>
    </div>
  );
}
