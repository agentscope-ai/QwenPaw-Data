import { useEffect, useState } from "react";
import { Input, Button, message, Tooltip } from "@/design";
import { CopyOutlined, RightOutlined, EditOutlined, SaveOutlined, CloseOutlined } from "@/design";
import { useTranslation } from "react-i18next";

import { explorerApi, kgApi } from "@/services/cmgraph";
import type { EntityUpdatePayload, EventUpdatePayload } from "@/services/cmgraph";
import type { GraphNode } from "../types";
import styles from "../index.module.less";
import { formatPropertyValue, copyProperty, copyAllProperties } from "../utils/propertyUtils";

interface NodePropertiesPanelProps {
  node: GraphNode;
  nodeColor: string;
  onClose: () => void;
  onNodeUpdate?: (nodeId: string, properties: Record<string, unknown>) => void;
}

export default function NodePropertiesPanel({
  node,
  nodeColor,
  onClose,
  onNodeUpdate,
}: NodePropertiesPanelProps) {
  const { t } = useTranslation();

  // Local copy of properties so we can reflect edits immediately
  const [localProps, setLocalProps] = useState<Record<string, unknown>>(node.properties ?? {});

  // Node detail (editable_fields) fetched from API
  const [editableFields, setEditableFields] = useState<string[]>([]);
  const [detailLoading, setDetailLoading] = useState(true);

  // Edit mode
  const [editing, setEditing] = useState(false);
  const [editValues, setEditValues] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);

  // Fetch node detail to get editable_fields
  useEffect(() => {
    let cancelled = false;
    setDetailLoading(true);
    explorerApi
      .getNodeDetail(node.id)
      .then((detail) => {
        if (cancelled) return;
        setEditableFields(detail.editable_fields ?? []);
        setLocalProps(detail.properties ?? node.properties ?? {});
      })
      .catch((err) => {
        if (cancelled) return;
        // Detail fetch failed — just use what we have from the graph node
        console.warn("Failed to fetch node detail:", err);
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false);
      });
    return () => { cancelled = true; };
  }, [node.id, node.properties]);

  // Reset local state when node changes
  useEffect(() => {
    setLocalProps(node.properties ?? {});
    setEditing(false);
    setEditValues({});
  }, [node.id, node.properties]);

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

  /** Save edited fields via KG API. */
  const handleSave = async () => {
    setSaving(true);
    try {
      // Build update payload from editable fields.
      // Convert string values back to their original types based on
      // the property's current value (e.g. arrays, numbers).
      const updates: Record<string, unknown> = {};
      for (const field of editableFields) {
        if (editValues[field] === undefined) continue;
        const raw = properties[field];
        if (Array.isArray(raw)) {
          // Parse comma-separated string back to array (e.g. aliases)
          updates[field] = editValues[field]
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean);
        } else if (typeof raw === "number") {
          const n = Number(editValues[field]);
          updates[field] = Number.isNaN(n) ? raw : n;
        } else if (typeof raw === "boolean") {
          updates[field] = editValues[field] === "true" || editValues[field] === "1";
        } else {
          updates[field] = editValues[field];
        }
      }

      // Determine whether this is an entity or event based on labels/type
      const isEvent = node.type === "Event" || node.type === "event";
      if (isEvent) {
        const payload: EventUpdatePayload = {
          name: (properties.canonical_name as string) || (properties.name as string) || node.label,
          type: node.type,
          ...updates,
        } as EventUpdatePayload;
        await kgApi.updateEvent(node.id, payload);
      } else {
        const payload: EntityUpdatePayload = {
          canonical_name: (properties.canonical_name as string) || node.label,
          type: node.type,
          ...updates,
        } as EntityUpdatePayload;
        await kgApi.updateEntity(node.id, payload);
      }

      // Merge updates into local properties
      const merged = { ...properties, ...updates };
      setLocalProps(merged);
      setEditing(false);
      setEditValues({});
      onNodeUpdate?.(node.id, merged);
      message.success(t("kgBrowser.editSuccess"));
    } catch (err) {
      console.error("Failed to update node:", err);
      message.error(t("kgBrowser.editFailed"));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className={styles.nodePropertiesPanel}>
      {/* Header */}
      <div className={styles.propertiesPanelHeader}>
        <span className={styles.overviewTitle}>
          {t("kgBrowser.nodeProperties")}
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

      {/* Node type tag */}
      <div className={styles.nodeTypeTag} style={{ background: nodeColor, color: "#fff" }}>
        {node.type}
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
                          setEditValues((prev) => ({ ...prev, [key]: e.target.value }))
                        }
                      />
                    ) : (
                      <Input
                        size="small"
                        value={editValues[key] ?? ""}
                        onChange={(e) =>
                          setEditValues((prev) => ({ ...prev, [key]: e.target.value }))
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
