import type { SemanticConfigPage } from '@/types/semanticConfig';
import type { UrlSearchValueParser } from './urlSearch';

export const parseSemanticDatasourceId: UrlSearchValueParser<string> = (value) => {
  const normalized = value.trim();
  return normalized || undefined;
};

/**
 * Semantic Config exposes BIGINT object IDs, but the frontend can only safely
 * address values that fit in JavaScript's integer range.
 */
export const parseSemanticObjectId: UrlSearchValueParser<number> = (value) => {
  const normalized = value.trim();
  if (!/^[1-9]\d*$/.test(normalized)) return undefined;
  const parsed = Number(normalized);
  return Number.isSafeInteger(parsed) ? parsed : undefined;
};

export interface SemanticDeepLinkMatch<T> {
  record: T;
  page: number;
  size: number;
  total: number;
}

interface LocateSemanticRecordOptions<T, TId extends string | number> {
  targetId: TId;
  getRecordId: (record: T) => TId;
  loadPage: (page: number, size: number) => Promise<SemanticConfigPage<T>>;
  size?: number;
}

/**
 * Locate an object through the documented list contract only. The scanner
 * advances using the response's total/page/size values and rejects a server
 * response for a page other than the one requested, preventing silent loops.
 */
export async function locateSemanticRecord<T, TId extends string | number>({
  targetId,
  getRecordId,
  loadPage,
  size = 20,
}: LocateSemanticRecordOptions<T, TId>): Promise<SemanticDeepLinkMatch<T> | undefined> {
  if (!Number.isSafeInteger(size) || size <= 0) {
    throw new Error('Semantic deep-link page size must be a positive safe integer');
  }

  let requestedPage = 1;
  while (true) {
    const result = await loadPage(requestedPage, size);
    if (result.page !== requestedPage || result.size <= 0) {
      throw new Error('Semantic deep-link pagination response does not match the requested page');
    }

    const record = result.records.find((item) => getRecordId(item) === targetId);
    if (record) {
      return {
        record,
        page: result.page,
        size: result.size,
        total: result.total,
      };
    }

    const lastPage = Math.max(1, Math.ceil(result.total / result.size));
    if (result.page >= lastPage) return undefined;
    requestedPage = result.page + 1;
  }
}

/** Run parent option loaders serially before issuing the child request. */
export async function loadSemanticDeepLinkInOrder<T>(
  loadParents: ReadonlyArray<() => Promise<unknown>>,
  loadChild: () => Promise<T>,
): Promise<T> {
  for (const loadParent of loadParents) {
    await loadParent();
  }
  return loadChild();
}

interface LoadSemanticRowsByParentOptions<
  TParent,
  TChild,
  TId extends string | number,
> extends LocateSemanticRecordOptions<TParent, TId> {
  loadRows: (parent: TParent) => Promise<SemanticConfigPage<TChild>>;
}

/** Resolve a parent object first, then preserve every paged row under it. */
export async function loadSemanticRowsByParent<TParent, TChild, TId extends string | number>({
  loadRows,
  ...locateOptions
}: LoadSemanticRowsByParentOptions<TParent, TChild, TId>): Promise<
  | {
      parent: SemanticDeepLinkMatch<TParent>;
      rows: SemanticConfigPage<TChild>;
    }
  | undefined
> {
  const parent = await locateSemanticRecord(locateOptions);
  if (!parent) return undefined;
  return { parent, rows: await loadRows(parent.record) };
}
