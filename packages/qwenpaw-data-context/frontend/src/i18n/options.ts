import type { TFunction } from 'i18next';

type LabelOption = {
  label?: unknown;
  value?: unknown;
  [key: string]: unknown;
};

export function translateOptionValue(
  t: TFunction,
  value: unknown,
  keyPrefix: string,
  fallback?: string,
) {
  if (value === null || value === undefined || value === '') return fallback ?? '-';
  const key = String(value);
  return t(`${keyPrefix}.${key}`, { defaultValue: fallback ?? key });
}

export function translateOptions<T extends LabelOption>(
  t: TFunction,
  options: T[],
  keyPrefix: string,
): T[] {
  return options.map((option) => {
    const rawKey = option.value ?? option.label;
    if (rawKey === null || rawKey === undefined) return option;

    return {
      ...option,
      label: translateOptionValue(
        t,
        rawKey,
        keyPrefix,
        typeof option.label === 'string' ? option.label : String(rawKey),
      ),
    };
  });
}
