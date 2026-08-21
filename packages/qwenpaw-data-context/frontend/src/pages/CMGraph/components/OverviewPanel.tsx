import { useMemo, useState } from "react";
import { Tag } from "@/design";
import { RightOutlined } from "@/design";
import { useTranslation } from "react-i18next";

import { NODE_TYPE_COLORS } from "../mock-data";
import type { QueryFrame } from "../types";
import styles from "../index.module.less";

interface OverviewPanelProps {
  frame: QueryFrame;
}

export default function OverviewPanel({ frame }: OverviewPanelProps) {
  const { t } = useTranslation();
  const [collapsed, setCollapsed] = useState(false);

  const { nodes, edges } = frame.result;

  // Dynamically compute node label statistics
  const nodeLabels = useMemo(() => {
    const map = new Map<string, number>();
    nodes.forEach((n) => map.set(n.type, (map.get(n.type) ?? 0) + 1));
    const items = Array.from(map, ([label, count]) => ({
      label,
      count,
      color: NODE_TYPE_COLORS[label] || "#78909c",
    }));
    items.unshift({ label: "*", count: nodes.length, color: "#78909c" });
    return items;
  }, [nodes]);

  // Dynamically compute relationship type statistics
  const relTypes = useMemo(() => {
    const map = new Map<string, number>();
    edges.forEach((e) => map.set(e.type, (map.get(e.type) ?? 0) + 1));
    const items = Array.from(map, ([type, count]) => ({ type, count }));
    items.unshift({ type: "*", count: edges.length });
    return items;
  }, [edges]);

  const totalNodes = nodes.length;
  const totalRels = edges.length;

  if (collapsed) {
    return (
      <div
        className={styles.overviewPanel}
        onClick={() => setCollapsed(false)}
        style={{ cursor: "pointer", padding: "12px 16px" }}
      >
        <span className={styles.overviewTitle}>
          {t("kgBrowser.overview")}
        </span>
        <RightOutlined style={{ fontSize: 12, marginLeft: 8 }} />
      </div>
    );
  }

  return (
    <div className={styles.overviewPanel}>
      <div
        className={styles.overviewHeader}
        onClick={() => setCollapsed(true)}
      >
        <span className={styles.overviewTitle}>
          {t("kgBrowser.overview")}
        </span>
        <RightOutlined style={{ fontSize: 12 }} />
      </div>

      <div className={styles.overviewSection}>
        <div className={styles.overviewLabel}>
          {t("kgBrowser.nodeLabels")}
        </div>
        <div className={styles.overviewTags}>
          {nodeLabels.map((item) => (
            <Tag key={item.label} color={item.color} className={styles.overviewTag}>
              {item.label === "*"
                ? `* (${item.count})`
                : `${item.label} (${item.count})`}
            </Tag>
          ))}
        </div>
      </div>

      <div className={styles.overviewSection}>
        <div className={styles.overviewLabel}>
          {t("kgBrowser.relationshipTypes")}
        </div>
        <div className={styles.overviewTags}>
          {relTypes.map((item) => (
            <Tag key={item.type} className={styles.overviewTag}>
              {item.type === "*"
                ? `* (${item.count})`
                : `${item.type} (${item.count})`}
            </Tag>
          ))}
        </div>
      </div>

      <div className={styles.overviewFooter}>
        {t("kgBrowser.displayingStats", {
          nodes: totalNodes,
          relationships: totalRels,
        })}
      </div>
    </div>
  );
}
