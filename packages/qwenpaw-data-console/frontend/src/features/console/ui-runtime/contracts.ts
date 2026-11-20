/** v1 contains navigation and unsaved drafts only. Never accept arbitrary URLs or selectors. */
export const PAGE_PATHS = {
  domains: '/business-domain', // Business domains.
  datasets: '/data-set', // Dataset catalogue.
  columns: '/column', // Columns.
  dimensions: '/dimension', // Dimensions.
  dimension_values: '/dimension-value', // Dimension values.
  dimension_calibers: '/dimension-caliber', // Dimension definitions.
  metrics: '/metric-lib', // Metrics.
  metric_formulas: '/metric-formula-lib', // Metric formulas.
  data_sources: '/data-source', // Sources.
  connections: '/data-connection', // Connections.
  weaving: '/semantic-weaving', // Manual weaving.
  profiling: '/data-profiling', // Profiling overview.
  profiling_detail: '/data-profiling/:datasourceId', // Explicit source only.
  kg_docs: '/kg-docs', // Documents.
  graph_dashboard: '/graph-center/dashboard', // Graph overview.
  graph_global: '/graph-center/global', // Global graph.
  graph_management: '/graph-management', // Graph management.
  graph_explore: '/graph-management/explore', // Specialized graph agent.
  model_settings: '/console/settings/agent', // Host model settings.
} as const;
export type PageId = keyof typeof PAGE_PATHS;
export type ToolName = 'ui_inspect' | 'ui_open_page' | 'ui_open_object' | 'ui_act';
export interface DatasetRef {
  kind: 'cm.dataset'; // Discriminator.
  datasource_id: string; // Authorized source identifier.
  domain_id: number; // Positive safe integer.
  dataset_id: number; // Positive safe integer.
}
export interface ArtifactRef {
  kind: 'artifact'; // Discriminator.
  session_id: string; // Owning session.
  artifact_id: string; // Existing artifact identifier.
}
export type ObjectRef = DatasetRef | ArtifactRef;
export interface UiAction {
  type: 'click' | 'focus' | 'clear' | 'fill' | 'select' | 'scroll' | 'set_state'; // Whitelisted operation.
  value?: string | string[]; // Only fill or select.
  anchor_id?: string; // Registered scroll anchor.
  tab_id?: string; // Registered tab.
  expanded?: boolean; // Explicit expansion state.
  visible?: boolean; // Explicit visibility state.
}
export interface UiArguments {
  scope?: 'current'; // Inspect scope.
  page_id?: PageId; // Registered navigation target.
  params?: Record<string, string>; // Page-specific, never arbitrary query parameters.
  object_ref?: ObjectRef; // Resolved business object.
  element_id?: string; // Registered local element.
  expected_view_revision?: string; // Compare before mutation.
  action?: UiAction; // Discriminated runtime-validated operation.
}
export interface UiElement {
  element_id: string; // Stable registry identifier, not a CSS selector.
  allowed_actions: UiAction['type'][]; // Explicit operations.
  object_ref?: ObjectRef; // Associated object.
  value_type?: 'text' | 'single_select' | 'multi_select'; // Input shape.
  options?: { id: string; /* Stable value. */ label: string /* Display only. */ }[]; // Real options.
}
export interface UiResult {
  object_ref?: ObjectRef; // Actual target.
  page_id?: PageId | 'manual_only'; // Current page.
  view_revision: string; // Current local revision.
  completed_steps: string[]; // Only actually completed steps.
  saved: false; // v1 never saves business data.
  view_state?: string; // Actual page state.
  visible?: boolean; // Actual visibility.
  value?: string | string[]; // Actual draft value.
  elements?: UiElement[]; // Authorized mounted handlers only.
}
export interface UiCall {
  session_id: string; // Original session.
  chat_id: string; // Original active turn.
  call_id: string; // Original model call.
  name: ToolName; // Deployed tool.
  arguments: UiArguments; // Strictly validated per tool.
  expires_at: string; // UTC deadline shared by execution and upload.
}
export interface UiError {
  code: string; // Stable failure code.
  message: string; // Human-readable failure.
}
export type UiOutcome =
  | { status: 'executed'; /* Terminal success. */ result: UiResult /* Actual result. */ }
  | {
      status: 'failed' | 'rejected';
      /* Terminal failure. */ error: UiError; /* Reason. */
    };

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value))
    throw new Error('INVALID_ARGUMENT');
  return value as Record<string, unknown>;
}
function exact(value: unknown, keys: string[]): Record<string, unknown> {
  const obj = record(value);
  if (Object.keys(obj).length !== keys.length || keys.some((key) => !(key in obj)))
    throw new Error('INVALID_ARGUMENT');
  return obj;
}
function text(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0;
}
export function validateObjectRef(value: unknown): ObjectRef {
  const obj = record(value);
  if (obj.kind === 'artifact') {
    exact(obj, ['kind', 'session_id', 'artifact_id']);
    if (!text(obj.session_id) || !text(obj.artifact_id)) throw new Error('INVALID_ARGUMENT');
  } else {
    exact(obj, ['kind', 'datasource_id', 'domain_id', 'dataset_id']);
    if (
      obj.kind !== 'cm.dataset' ||
      !text(obj.datasource_id) ||
      !Number.isSafeInteger(obj.domain_id) ||
      Number(obj.domain_id) <= 0 ||
      !Number.isSafeInteger(obj.dataset_id) ||
      Number(obj.dataset_id) <= 0
    )
      throw new Error('INVALID_ARGUMENT');
  }
  return obj as unknown as ObjectRef;
}
export function validateArguments(name: string, value: unknown): UiArguments {
  const obj = record(value);
  if (name === 'ui_inspect') {
    exact(obj, ['scope']);
    if (obj.scope !== 'current') throw new Error('INVALID_ARGUMENT');
  } else if (name === 'ui_open_page') {
    exact(obj, ['page_id', 'params']);
    if (
      typeof obj.page_id !== 'string' ||
      !Object.prototype.hasOwnProperty.call(PAGE_PATHS, obj.page_id)
    )
      throw new Error('INVALID_ARGUMENT');
    const params = exact(obj.params, obj.page_id === 'profiling_detail' ? ['datasourceId'] : []);
    if (obj.page_id === 'profiling_detail' && !text(params.datasourceId))
      throw new Error('INVALID_ARGUMENT');
  } else if (name === 'ui_open_object') {
    exact(obj, ['object_ref']);
    validateObjectRef(obj.object_ref);
  } else if (name === 'ui_act') {
    exact(obj, ['element_id', 'expected_view_revision', 'action']);
    if (!text(obj.element_id) || !text(obj.expected_view_revision))
      throw new Error('INVALID_ARGUMENT');
    const action = record(obj.action);
    switch (action.type) {
      case 'click':
      case 'focus':
      case 'clear':
        exact(action, ['type']);
        break;
      case 'fill':
        exact(action, ['type', 'value']);
        if (typeof action.value !== 'string') throw new Error('INVALID_ARGUMENT');
        break;
      case 'select':
        exact(action, ['type', 'value']);
        if (!Array.isArray(action.value) || !action.value.every(text))
          throw new Error('INVALID_ARGUMENT');
        break;
      case 'scroll':
        exact(action, ['type', 'anchor_id']);
        if (!text(action.anchor_id)) throw new Error('INVALID_ARGUMENT');
        break;
      case 'set_state': {
        const key = ['tab_id', 'visible', 'expanded'].find((candidate) => candidate in action);
        if (!key) throw new Error('INVALID_ARGUMENT');
        exact(action, ['type', key]);
        if (key === 'tab_id' ? !text(action[key]) : typeof action[key] !== 'boolean')
          throw new Error('INVALID_ARGUMENT');
        break;
      }
      default:
        throw new Error('INVALID_ARGUMENT');
    }
  } else throw new Error('UNKNOWN_TOOL');
  return obj as UiArguments;
}

const TOOL_NAMES = new Set<ToolName>(['ui_inspect', 'ui_open_page', 'ui_open_object', 'ui_act']);

export interface UiCallSource {
  object?: string;
  type?: string;
  status?: string;
  chat_id?: string | null;
  source_id?: string | null;
  sequence?: number;
  content?: Array<{ type?: string; data?: unknown }>;
  metadata?: Record<string, unknown> | null;
}

function payloadOf(event: UiCallSource): Record<string, unknown> | null {
  const data = event.content?.find((item) => item.type === 'data')?.data;
  if (!data || typeof data !== 'object' || Array.isArray(data)) return null;
  return data as Record<string, unknown>;
}

function callIdOf(event: UiCallSource): string | null {
  const callId = payloadOf(event)?.call_id;
  return text(callId) ? callId : text(event.source_id) ? event.source_id : null;
}

function hasUiResult(messages: UiCallSource[], chatId: string, callId: string): boolean {
  return messages.some(
    (message) =>
      message.chat_id === chatId &&
      message.type === 'plugin_call_output' &&
      callIdOf(message) === callId &&
      (message.status === 'completed' ||
        message.status === 'failed' ||
        message.status === 'cancelled'),
  );
}

export function readUiCall(event: UiCallSource, sessionId: string): UiCall | null {
  if (event.object != null && event.object !== 'message') return null;
  if (event.type !== 'plugin_call' || event.status !== 'completed') return null;
  if (event.metadata?.awaiting_ui !== true) return null;
  if (!text(sessionId) || !text(event.chat_id)) throw new Error('INVALID_ARGUMENT');
  const payload = payloadOf(event);
  if (!payload) throw new Error('INVALID_ARGUMENT');
  const name = payload.name;
  const callId = payload.call_id;
  if (typeof name !== 'string' || !text(callId)) throw new Error('INVALID_ARGUMENT');
  if (!TOOL_NAMES.has(name as ToolName)) throw new Error('UNKNOWN_TOOL');
  const expiresAt = event.metadata.expires_at;
  if (!text(expiresAt)) throw new Error('INVALID_ARGUMENT');
  let args: unknown = payload.arguments;
  if (typeof args === 'string') {
    try {
      args = JSON.parse(args);
    } catch {
      throw new Error('INVALID_ARGUMENT');
    }
  }
  return {
    session_id: sessionId,
    chat_id: event.chat_id,
    call_id: callId,
    name: name as ToolName,
    arguments: (args && typeof args === 'object' && !Array.isArray(args)
      ? args
      : {}) as UiArguments,
    expires_at: expiresAt,
  };
}

export function selectPendingUiCalls(
  messages: UiCallSource[],
  chat: { id: string; session_id: string },
): UiCall[] {
  const ordered = [...messages].sort((left, right) => (left.sequence ?? 0) - (right.sequence ?? 0));
  const calls: UiCall[] = [];
  const seen = new Set<string>();
  for (const message of ordered) {
    if (message.chat_id !== chat.id) continue;
    const call = readUiCall(message, chat.session_id);
    if (!call || seen.has(call.call_id) || hasUiResult(ordered, chat.id, call.call_id)) continue;
    seen.add(call.call_id);
    calls.push(call);
  }
  return calls;
}
