export type UrlSearchValueParser<T> = (value: string) => T | undefined;

export interface UrlSearchField<T> {
  /** Query-string key. Defaults to the target object field name. */
  param?: string;
  /** Backward-compatible query-string keys, checked after the canonical key. */
  aliases?: readonly string[];
  parse?: UrlSearchValueParser<T>;
}

export type UrlSearchSchema<T extends object> = {
  [K in keyof T]?: UrlSearchField<T[K]>;
};

export const parseUrlString: UrlSearchValueParser<string> = (value) => {
  const normalized = value.trim();
  return normalized || undefined;
};

export const parseUrlNumber: UrlSearchValueParser<number> = (value) => {
  if (!value.trim()) return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
};

export function createUrlEnumParser<const T extends string>(
  values: readonly T[],
): UrlSearchValueParser<T> {
  const allowedValues = new Set<string>(values);
  return (value) => (allowedValues.has(value) ? (value as T) : undefined);
}

/**
 * Read a page's supported table filters from the URL.
 *
 * The explicit schema keeps route-only parameters and unknown query keys out of
 * API requests while allowing old links to keep working through aliases.
 */
export function readUrlSearchValues<T extends object>(
  searchParams: URLSearchParams,
  schema: UrlSearchSchema<T>,
): Partial<T> {
  const result: Partial<T> = {};

  for (const key of Object.keys(schema) as Array<keyof T>) {
    const field = schema[key];
    if (!field) continue;

    const paramNames = [field.param ?? String(key), ...(field.aliases ?? [])];
    let rawValue: string | null = null;
    for (const paramName of paramNames) {
      if (searchParams.has(paramName)) {
        rawValue = searchParams.get(paramName);
        break;
      }
    }
    if (rawValue === null) continue;

    const parser = field.parse ?? parseUrlString;
    const parsedValue = parser(rawValue);
    if (parsedValue !== undefined) {
      result[key] = parsedValue as T[keyof T];
    }
  }

  return result;
}
