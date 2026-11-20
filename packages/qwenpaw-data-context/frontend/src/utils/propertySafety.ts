const RESERVED_GRAPH_PROPERTY_KEYS = new Set(['workspace_id', 'owner_id', 'key']);

export const RESERVED_MG_APPEND_PROPERTY_KEYS = new Set([
  ...RESERVED_GRAPH_PROPERTY_KEYS,
  'mg_origin',
  'mg_keep_user_digest',
]);

export function filterGraphProperties(
  properties: Record<string, unknown> | null | undefined,
): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(properties ?? {}).filter(([key]) => !RESERVED_GRAPH_PROPERTY_KEYS.has(key)),
  );
}

export function filterEditableGraphFields(fields: string[] | null | undefined): string[] {
  return (fields ?? []).filter((field) => !RESERVED_GRAPH_PROPERTY_KEYS.has(field));
}

export function filterMgAppendProperties(
  properties: Record<string, unknown>,
): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(properties).filter(([key]) => !RESERVED_MG_APPEND_PROPERTY_KEYS.has(key)),
  );
}
