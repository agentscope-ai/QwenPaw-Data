import type {
  ConfirmWeavingParams,
  SemanticWeavingTaskQueryParams,
  WeavingTaskStatus,
} from '@/types/semanticWeaving';

export const SEMANTIC_WEAVING_DEFAULT_PAGE = 1;
export const SEMANTIC_WEAVING_DEFAULT_PAGE_SIZE = 20;
export const SEMANTIC_WEAVING_POLL_INTERVAL_MS = 15_000;
export const SEMANTIC_WEAVING_PROTOCOL_ERROR_COLOR = '#ff4d4f';

export const SEMANTIC_WEAVING_TASK_STATUSES = [
  'QUEUED',
  'RUNNING',
  'SUCCESS',
  'DEGRADED',
  'FAILED',
  'KILLED',
] as const satisfies readonly WeavingTaskStatus[];

const TASK_STATUS_SET = new Set<string>(SEMANTIC_WEAVING_TASK_STATUSES);
const KILLABLE_TASK_STATUS_SET = new Set<WeavingTaskStatus>(['QUEUED', 'RUNNING']);

export interface SemanticWeavingSubmitValues {
  datasource_id: string;
  task_name: string;
}

export interface SemanticWeavingTableQueryInput extends Record<string, unknown> {
  current?: number;
  pageSize?: number;
  datasource_name?: unknown;
  task_name?: unknown;
}

export type SemanticWeavingStatusView =
  | {
      kind: 'known';
      status: WeavingTaskStatus;
      translationKey: `semanticWeaving.status.${WeavingTaskStatus}`;
    }
  | {
      kind: 'protocol-error';
      rawStatus: string;
      translationKey: 'semanticWeaving.status.protocolError';
    };

export type SemanticWeavingSubmitFeedback = {
  kind: 'success' | 'error' | 'warning';
  translationKey:
    | 'semanticWeaving.submitSuccess'
    | 'semanticWeaving.submitCompleted'
    | 'semanticWeaving.submitDegraded'
    | 'semanticWeaving.submitFailed'
    | 'semanticWeaving.submitKilled'
    | 'semanticWeaving.submitUnexpectedStatus';
};

function toOptionalText(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  const trimmed = value.trim();
  return trimmed || undefined;
}

function toPositiveInteger(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isInteger(value) && value > 0 ? value : fallback;
}

export function buildSemanticWeavingSubmitParams(
  values: SemanticWeavingSubmitValues,
): ConfirmWeavingParams {
  return {
    datasource_id: values.datasource_id.trim(),
    task_name: values.task_name.trim(),
    weave_mode: 'FULL',
  };
}

export function buildSemanticWeavingTaskQuery(
  input: SemanticWeavingTableQueryInput,
): SemanticWeavingTaskQueryParams {
  const query: SemanticWeavingTaskQueryParams = {
    page: toPositiveInteger(input.current, SEMANTIC_WEAVING_DEFAULT_PAGE),
    size: toPositiveInteger(input.pageSize, SEMANTIC_WEAVING_DEFAULT_PAGE_SIZE),
  };
  const datasourceName = toOptionalText(input.datasource_name);
  const taskName = toOptionalText(input.task_name);
  if (datasourceName) query.datasource_name = datasourceName;
  if (taskName) query.task_name = taskName;
  return query;
}

export function getSemanticWeavingStatusView(status: unknown): SemanticWeavingStatusView {
  if (typeof status === 'string' && TASK_STATUS_SET.has(status)) {
    const knownStatus = status as WeavingTaskStatus;
    return {
      kind: 'known',
      status: knownStatus,
      translationKey: `semanticWeaving.status.${knownStatus}`,
    };
  }
  const rawStatus =
    typeof status === 'string' && status.trim() ? status.trim() : String(status ?? '-');
  return {
    kind: 'protocol-error',
    rawStatus,
    translationKey: 'semanticWeaving.status.protocolError',
  };
}

export function canKillSemanticWeavingTask(status: unknown): boolean {
  return (
    typeof status === 'string' &&
    TASK_STATUS_SET.has(status) &&
    KILLABLE_TASK_STATUS_SET.has(status as WeavingTaskStatus)
  );
}

export function getSemanticWeavingSubmitFeedback(status: unknown): SemanticWeavingSubmitFeedback {
  switch (status) {
    case 'SUCCESS':
      return { kind: 'success', translationKey: 'semanticWeaving.submitCompleted' };
    case 'DEGRADED':
      return { kind: 'warning', translationKey: 'semanticWeaving.submitDegraded' };
    case 'FAILED':
      return { kind: 'error', translationKey: 'semanticWeaving.submitFailed' };
    case 'KILLED':
      return { kind: 'warning', translationKey: 'semanticWeaving.submitKilled' };
    case 'QUEUED':
    case 'RUNNING':
      return { kind: 'success', translationKey: 'semanticWeaving.submitSuccess' };
    default:
      return { kind: 'warning', translationKey: 'semanticWeaving.submitUnexpectedStatus' };
  }
}
