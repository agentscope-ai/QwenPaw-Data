import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { QueryFrame } from "../types";
import styles from "../index.module.less";

interface CollapsibleRowProps {
  label: string;
  value: string;
  expandedContent: string;
}

function CollapsibleRow({ label, value, expandedContent }: CollapsibleRowProps) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className={styles.codeViewCollapsible}>
      <div
        className={styles.codeViewRow}
        onClick={() => setExpanded(!expanded)}
      >
        <span className={styles.codeViewKey}>
          {label} {expanded ? "▾" : "▸"}
        </span>
        <span className={styles.codeViewValue}>
          {expanded ? "" : value}
        </span>
      </div>
      {expanded && (
        <pre className={styles.codeViewExpandedContent}>
          {expandedContent}
        </pre>
      )}
    </div>
  );
}

interface CodeViewProps {
  frame: QueryFrame;
}

export default function CodeView({ frame }: CodeViewProps) {
  const { t } = useTranslation();
  const meta = frame.meta;
  const rows = frame.rows ?? [];

  // Build summary object from meta — includes labels_hit and elapsed_ms
  const summaryObj = meta
    ? {
        query: { text: meta.query },
        queryType: meta.queryType ?? "r",
        counters: meta.counters ?? {},
        labels_hit: meta.labelsHit ?? [],
        elapsed_ms: meta.elapsedMs ?? 0,
      }
    : null;

  // Build response object (keys + length)
  const responseObj = {
    keys: frame.columns ?? [],
    length: meta?.totalRecords ?? rows.length,
  };

  // Rows JSON for raw data display
  const rowsJson = JSON.stringify(rows, null, 2);
  const rowsPreview = rows.length > 0 && rows[0]
    ? `[{ ${Object.keys(rows[0]).slice(0, 2).join(", ")}${Object.keys(rows[0]).length > 2 ? ", ..." : ""} }, ...] (${rows.length})`
    : "[]";

  return (
    <div className={styles.codeView}>
      {/* Meta rows */}
      <div className={styles.codeViewBody}>
        {meta ? (
          <>
            <div className={styles.codeViewRow}>
              <span className={styles.codeViewKey}>
                {t("kgBrowser.serverVersion")}
              </span>
              <span className={styles.codeViewValue}>{meta.serverVersion ?? "N/A"}</span>
            </div>
            <div className={styles.codeViewRow}>
              <span className={styles.codeViewKey}>
                {t("kgBrowser.serverAddress")}
              </span>
              <span className={styles.codeViewValue}>{meta.serverAddress ?? "N/A"}</span>
            </div>
            <div className={styles.codeViewRow}>
              <span className={styles.codeViewKey}>
                {t("kgBrowser.query")}
              </span>
              <span className={styles.codeViewValue}>{meta.query}</span>
            </div>
          </>
        ) : (
          <div className={styles.codeViewEmpty}>
            {t("kgBrowser.noMetadata")}
          </div>
        )}

        {/* Summary: query meta info (node/edge count, labels hit, elapsed time) */}
        {summaryObj && (
          <CollapsibleRow
            label={t("kgBrowser.summary")}
            value={`{ "query": {...}, "counters": {...}, "labels_hit": [...], ... }`}
            expandedContent={JSON.stringify(summaryObj, null, 2)}
          />
        )}

        {/* Response: keys + length */}
        <CollapsibleRow
          label={t("kgBrowser.response")}
          value={`[{ "keys": [...] }], length: ${responseObj.length}`}
          expandedContent={JSON.stringify(responseObj, null, 2)}
        />

        {/* Rows: raw JSON data */}
        <CollapsibleRow
          label={t("kgBrowser.rows")}
          value={rowsPreview}
          expandedContent={rowsJson}
        />
      </div>

      {/* Status bar */}
      <div className={styles.viewStatusBar}>
        {meta
          ? t("kgBrowser.streamingStatus", {
              records: meta.totalRecords ?? rows.length,
              startMs: meta.startedAfterMs ?? 0,
              endMs: meta.completedAfterMs ?? 0,
            })
          : t("kgBrowser.streamingStatus", {
              records: rows.length,
              startMs: 0,
              endMs: 0,
            })}
      </div>
    </div>
  );
}
