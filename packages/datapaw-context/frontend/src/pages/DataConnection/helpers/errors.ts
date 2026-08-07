import type { TFunction } from "i18next";
import type { DataSourceRecord, DataSourceTestResult } from "@/services/dataConnection";

const ERROR_CODE_ALIASES: Record<string, string> = {
  name_conflict: "nameConflict",
  conflict: "nameConflict",
  duplicate: "nameConflict",
  connection_rejected: "connectionRejected",
  unauthorized: "connectionRejected",
  auth_failed: "connectionRejected",
  not_found: "notFound",
  404: "notFound",
};

function normalizeCode(value?: string | null): string | undefined {
  if (!value) return undefined;
  const raw = value.trim();
  if (!raw) return undefined;
  return ERROR_CODE_ALIASES[raw] ?? ERROR_CODE_ALIASES[raw.toLowerCase()] ?? raw;
}

/** Resolve backend or thrown errors into an i18n error code suffix. */
export function resolveApiErrorCode(error: unknown, fallback = "requestFailed"): string {
  if (typeof error === "string") return normalizeCode(error) ?? fallback;

  if (error instanceof Error) {
    return normalizeCode(error.message) ?? fallback;
  }

  if (error && typeof error === "object") {
    const body = error as Record<string, unknown>;
    for (const key of ["code", "error", "message", "detail"]) {
      const value = body[key];
      if (typeof value === "string") {
        return normalizeCode(value) ?? fallback;
      }
    }
  }

  return fallback;
}

/** Translate a data-connection error code, falling back to a generic key. */
export function resolveErrorMessage(
  t: TFunction,
  code: string,
  fallbackKey = "dataConnection.errors.requestFailed",
): string {
  const key = `dataConnection.errors.${code}`;
  return t(key, { defaultValue: t(fallbackKey, { defaultValue: code }) });
}

export function formatTestSuccessMessage(t: TFunction, result: DataSourceTestResult): string {
  const latency = result.latencyMs ?? 0;
  return t("dataConnection.testSuccess", {
    message: result.message || t("dataConnection.errors.connectionOk"),
    latency,
  });
}

export function formatCreateSuccessMessage(
  t: TFunction,
  record: Pick<DataSourceRecord, "name">,
): string {
  if (record.name) {
    return t("dataConnection.addSuccessWithName", {
      name: record.name,
      defaultValue: t("dataConnection.addSuccess"),
    });
  }
  return t("dataConnection.addSuccess");
}
