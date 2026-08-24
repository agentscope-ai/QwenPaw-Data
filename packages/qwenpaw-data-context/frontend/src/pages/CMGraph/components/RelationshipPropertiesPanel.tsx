import { useEffect, useState } from "react";
import { Input, Button, message, Tooltip } from "@/design";
import {
  CopyOutlined,
  RightOutlined,
  EditOutlined,
  SaveOutlined,
  CloseOutlined,
} from "@/design";
import { useTranslation } from "react-i18next";

import { explorerApi, kgApi } from "@/services/cmgraph";
import type { GraphEdge } from "../types";
import styles from "../index.module.less";
import { formatPropertyValue, copyProperty, copyAllProperties } from "../utils/propertyUtils";

interface RelationshipPropertiesPanelProps {
  edge: GraphEdge;
  onClose: () => void;
  onEdgeUpdate?: (edgeId: string, properties: Record<string, unknown>) => void;
}

export default function RelationshipPropertiesPanel({
  edge,
  onClose,
  onEdgeUpdate,
}: RelationshipPropertiesPanelProps) {
  const { t } = useTranslation();

  // Local copy of properties so we can reflect edits immediately
  const [localProps, setLocalProps] = useState<Record<string, unknown>>(
    edge.properties ?? {},
  );

  // Edge detail (editable_fields) fetched from API
  const [editableFields, setEditableFields] = useState<string[]>([]);
  const [detailLoading, setDetailLoading] = useState(true);

  // Edit mode
  const [editing, setEditing] = useState(false);
  const [editValues, setEditValues] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);

  // Fetch edge detail to get editable_fields
  useEffect(() => {
    let cancelled = false;
    setDetailLoading(true);
    explorerApi
      .getEdgeDetail({
        source_key: edge.source,
        target_key: edge.target,
        rel_type: edge.type,
      })
      .then((detail) => {
        if (cancelled) return;
        setEditableFields(detail.editable_fields ?? []);
        setLocalProps(detail.properties ?? edge.properties ?? {});
      })
      .catch((err) => {
        if (cancelled) return;
        // Detail fetch failed — just use what we have from the graph edge
        console.warn("Failed to fetch edge detail:", err);
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [edge.source, edge.target, edge.type, edge.properties]);

  // Reset local state when edge changes
  useEffect(() => {
    setLocalProps(edge.properties ?? {});
    setEditing(false);
    setEditValues({});
  }, [edge.id, edge.properties]);

  const properties = localProps;
  const entries = Object.entries(properties);
  const hasEditable = editableFields.length > 0;

  /** Enter edit mode: initialise edit values from current properties. */
  const handleStartEdit = () => {
    const initial: Record<string, string> = {};
    for (const field of editableFields) {
      initial[field] = formatPropertyValue(properties[field] ?? "");
    }
    setEditValues(initial);
    setEditing(true);
  };

  /** Cancel editing and discard changes. */
  const handleCancelEdit = () => {
    setEditing(false);
    setEditValues({});
  };

  /** Save edited fields via KG edge properties API. */
  const handleSave = async () => {
    setSaving(true);
    try {
      // Build update payload from editable fields
      const updates: Record<string, unknown> = {};
      for (const field of editableFields) {
        if (editValues[field] !== undefined) {
          updates[field] = editValues[field];
        }
      }

      await kgApi.updateEdgeProperties({
        from_key: edge.source,
        to_key: edge.target,
        rel_type: edge.type,
        properties: updates,
      });

      // Merge updates into local properties
      const merged = { ...properties, ...updates };
      setLocalProps(merged);
      setEditing(false);
      setEditValues({});
      onEdgeUpdate?.(edge.id, merged);
      message.success(t("kgBrowser.edgeEditSuccess"));
    } catch (err) {
      console.error("Failed to update edge properties:", err);
      message.error(t("kgBrowser.edgeEditFailed"));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className={styles.nodePropertiesPanel}>
      {/* Header */}
      <div className={styles.propertiesPanelHeader}>
        <span className={styles.overviewTitle}>
          {t("kgBrowser.relationshipProperties")}
        </span>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {!editing && hasEditable && !detailLoading && (
            <Tooltip title={t("kgBrowser.edit")}>
              <EditOutlined
                style={{ fontSize: 13, cursor: "pointer" }}
                onClick={handleStartEdit}
              />
            </Tooltip>
          )}
          <CopyOutlined
            className={styles.copyBtn}
            onClick={() => copyAllProperties(properties, t)}
            title={t("kgBrowser.copyProperty")}
          />
          <RightOutlined
            style={{ fontSize: 12, cursor: "pointer" }}
            onClick={onClose}
          />
        </div>
      </div>

      {/* Relationship type tag */}
      <div
        className={styles.nodeTypeTag}
        style={{ background: "#4a4a4a", color: "#fff" }}
      >
        {edge.type}
      </div>

      {/* Properties list */}
      <div className={styles.propertiesPanelContent}>
        {entries.length === 0 ? (
          <div style={{ color: "var(--colorTextSecondary, #999)", fontSize: 13 }}>
            {t("kgBrowser.noProperties")}
          </div>
        ) : (
          entries.map(([key, value]) => {
            const isEditable = editableFields.includes(key);
            const isEditingThis = editing && isEditable;

            return (
              <div key={key} className={styles.propertyRow}>
                <span className={styles.propertyKey}>
                  {key.startsWith("element") || key === "id"
                    ? `<${key}>`
                    : key}
                </span>
                {isEditingThis ? (
                  <div className={styles.propertyEditValue}>
                    {typeof value === "string" && value.length > 50 ? (
                      <Input.TextArea
                        size="small"
                        autoSize={{ minRows: 2, maxRows: 6 }}
                        value={editValues[key] ?? ""}
                        onChange={(e) =>
                          setEditValues((prev) => ({
                            ...prev,
                            [key]: e.target.value,
                          }))
                        }
                      />
                    ) : (
                      <Input
                        size="small"
                        value={editValues[key] ?? ""}
                        onChange={(e) =>
                          setEditValues((prev) => ({
                            ...prev,
                            [key]: e.target.value,
                          }))
                        }
                      />
                    )}
                  </div>
                ) : (
                  <span className={styles.propertyValue}>
                    <span>{formatPropertyValue(value)}</span>
                    {isEditable && !editing && (
                      <EditOutlined
                        style={{ fontSize: 11, cursor: "pointer", marginLeft: 4 }}
                        onClick={handleStartEdit}
                      />
                    )}
                    <CopyOutlined
                      className={styles.copyBtn}
                      onClick={() => copyProperty(key, value, t)}
                    />
                  </span>
                )}
              </div>
            );
          })
        )}
      </div>

      {/* Footer: Save / Cancel buttons in edit mode */}
      {editing && (
        <div className={styles.propertiesPanelFooter}>
          <Button
            size="small"
            icon={<CloseOutlined />}
            onClick={handleCancelEdit}
            disabled={saving}
          >
            {t("kgBrowser.cancel")}
          </Button>
          <Button
            type="primary"
            size="small"
            icon={<SaveOutlined />}
            onClick={handleSave}
            loading={saving}
          >
            {t("kgBrowser.save")}
          </Button>
        </div>
      )}
    </div>
  );
}
