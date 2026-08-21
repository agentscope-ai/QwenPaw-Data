import { useMemo } from "react";
import type { ReactNode } from "react";
import { Table, Tag, Tooltip } from "@/design";
import type { TableProps } from "@/design";
import { CopyOutlined } from "@/design";
import { useTranslation } from "react-i18next";
import type { QueryFrame } from "../types";
import styles from "../index.module.less";
import { copyToClipboard } from "../utils/clipboard";

interface TableViewProps {
  frame: QueryFrame;
}

type TableRow = Record<string, unknown> & {
  __rowKey: string;
  __rowIndex: number;
};

/** Check if a value is a Neo4j-like node/relationship object. */
function isNodeLike(value: unknown): boolean {
  if (typeof value !== "object" || value === null || Array.isArray(value))
    return false;
  const obj = value as Record<string, unknown>;
  return (
    "labels" in obj ||
    "properties" in obj ||
    "identity" in obj ||
    "type" in obj
  );
}

/** Format a value into a compact string for summaries. */
function formatScalar(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

/** Render a cell value with proper formatting for the table. */
function renderCellValue(value: unknown): ReactNode {
  if (value === null || value === undefined) {
    return <span className={styles.tableCellNull}>—</span>;
  }
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean")
    return String(value);

  // Object or array — check if it's a node-like object
  if (isNodeLike(value)) {
    const obj = value as {
      labels?: string[];
      properties?: Record<string, unknown>;
      type?: string;
    };
    const labels = obj.labels ?? (obj.type ? [obj.type] : []);
    const props = obj.properties ?? {};
    const propEntries = Object.entries(props);
    const propSummary = propEntries
      .slice(0, 3)
      .map(([k, v]) => `${k}: ${typeof v === "string" ? `"${v}"` : formatScalar(v)}`)
      .join(", ");
    const moreCount =
      propEntries.length > 3 ? `, +${propEntries.length - 3}` : "";
    return (
      <div className={styles.tableCellObject}>
        {labels.length > 0 && (
          <span className={styles.tableCellLabels}>
            {labels.map((label) => (
              <Tag key={label} className={styles.tableCellTag}>
                {label}
              </Tag>
            ))}
          </span>
        )}
        {propEntries.length > 0 && (
          <code className={styles.tableCellProps}>
            {`{${propSummary}${moreCount}}`}
          </code>
        )}
      </div>
    );
  }

  // Generic object/array — show truncated JSON
  const jsonStr = formatScalar(value);
  const display =
    jsonStr.length > 80 ? jsonStr.slice(0, 80) + "…" : jsonStr;
  return <code className={styles.tableCellJson}>{display}</code>;
}

export default function TableView({ frame }: TableViewProps) {
  const { t } = useTranslation();

  // Table view uses ONLY rows + columns from the response.
  const records = useMemo(() => frame.rows ?? [], [frame.rows]);
  const meta = frame.meta;
  const columns = useMemo(
    () => frame.columns?.length
      ? frame.columns
      : records.length > 0
        ? Object.keys(records[0])
        : [],
    [frame.columns, records],
  );

  // Build Antd Table columns
  const tableColumns = useMemo(() => {
    return columns.map((col) => ({
      title: col,
      dataIndex: col,
      key: col,
      render: (value: unknown) => renderCellValue(value),
    }));
  }, [columns]);

  // Build data source with stable keys
  const dataSource = useMemo(() => {
    return records.map((record, index) => ({
      ...record,
      __rowKey: String(index),
      __rowIndex: index,
    }));
  }, [records]);

  // Pre-compute full record JSON for copy in expanded rows
  const recordTexts = useMemo(
    () => records.map((r) => JSON.stringify(r, null, 2)),
    [records],
  );

  // Expandable row — show full JSON
  const expandedRowRender = (record: TableRow) => {
    const idx = record.__rowIndex;
    return (
      <div className={styles.tableExpandedRow}>
        <div className={styles.tableExpandedHeader}>
          <span className={styles.tableExpandedLabel}>
            {t("kgBrowser.recordJson")}
          </span>
          <Tooltip title={t("kgBrowser.copyRecord")}>
            <button
              className={styles.tableExpandedCopyBtn}
              onClick={() => copyToClipboard(recordTexts[idx] ?? "", t)}
            >
              <CopyOutlined />
            </button>
          </Tooltip>
        </div>
        <pre className={styles.tableExpandedJson}>
          {recordTexts[idx]}
        </pre>
      </div>
    );
  };

  const tableProps: TableProps<TableRow> = {
    columns: tableColumns,
    dataSource,
    rowKey: "__rowKey",
    size: "small",
    pagination: false,
    scroll: { x: "max-content" },
    expandable: {
      expandedRowRender,
      rowExpandable: (record) =>
        columns.some((col) => {
          const val = (record as Record<string, unknown>)[col];
          return val !== null && typeof val === "object";
        }),
    },
  };

  return (
    <div className={styles.tableView}>
      {/* Single card containing the entire table */}
      <div className={styles.tableCard}>
        {records.length === 0 ? (
          <div className={styles.tableEmpty}>{t("kgBrowser.noRecords")}</div>
        ) : (
          <Table<TableRow> {...tableProps} />
        )}
      </div>

      {/* Status bar */}
      <div className={styles.viewStatusBar}>
        {meta
          ? t("kgBrowser.streamingStatus", {
              records: meta.totalRecords ?? records.length,
              startMs: meta.startedAfterMs ?? 0,
              endMs: meta.completedAfterMs ?? 0,
            })
          : t("kgBrowser.streamingStatus", {
              records: records.length,
              startMs: 0,
              endMs: 0,
            })}
      </div>
    </div>
  );
}
