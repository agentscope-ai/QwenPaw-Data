import type { TFunction } from "i18next";
import { copyToClipboard } from "./clipboard";

export function formatPropertyValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value)) return `[${value.join(", ")}]`;
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** 复制单个属性（key + value） */
export function copyProperty(key: string, value: unknown, t: TFunction): void {
  const text = `${key}: ${formatPropertyValue(value)}`;
  copyToClipboard(text, t);
}

/** 复制全部属性（多行） */
export function copyAllProperties(
  properties: Record<string, unknown>,
  t: TFunction,
): void {
  const text = Object.entries(properties)
    .map(([k, v]) => `${k}: ${formatPropertyValue(v)}`)
    .join("\n");
  copyToClipboard(text, t);
}
