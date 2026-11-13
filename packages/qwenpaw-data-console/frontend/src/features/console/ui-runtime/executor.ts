import type { UiCall, UiOutcome, UiResult } from './contracts.js';
import type { ExecutionContext } from './registry.js';

const REJECTED = new Set([
  'INVALID_ARGUMENT',
  'FORBIDDEN',
  'USER_EDIT_CONFLICT',
  'VIEW_STALE',
  'ACTION_NOT_ALLOWED',
  'TARGET_NOT_FOUND',
]);
const DOMAIN = new Set([...REJECTED, 'MODULE_NOT_READY', 'UNKNOWN_TOOL', 'CONTEXT_CHANGED']);
const HTTP_ERRORS = new Map([
  [400, 'INVALID_ARGUMENT'],
  [401, 'FORBIDDEN'],
  [403, 'FORBIDDEN'],
  [404, 'TARGET_NOT_FOUND'],
  [422, 'INVALID_ARGUMENT'],
]);

export interface ExecutorDependencies {
  execute: (call: UiCall, context: ExecutionContext) => Promise<UiResult>;
  answer: (call: UiCall, outcome: UiOutcome, signal: AbortSignal) => Promise<unknown>;
  current: (call: UiCall) => boolean;
}
export class UiExecutor {
  private queue: Promise<void> = Promise.resolve();
  private cache = new Map<string, Promise<void>>();
  private cancellation = new AbortController();
  constructor(private deps: ExecutorDependencies) {}
  cancel() {
    this.cancellation.abort();
  }
  accept(call: UiCall): Promise<void> {
    if (!this.deps.current(call)) return Promise.resolve();
    const existing = this.cache.get(call.call_id);
    if (existing) return existing;
    const task = this.queue.then(() => this.run(call));
    this.cache.set(call.call_id, task);
    this.queue = task.catch(() => undefined);
    return task;
  }
  private async run(call: UiCall) {
    const budget = Date.parse(call.expires_at) - Date.now();
    if (!Number.isFinite(budget) || budget <= 0 || this.cancellation.signal.aborted) return;
    const controller = new AbortController();
    const abort = () => controller.abort();
    this.cancellation.signal.addEventListener('abort', abort, { once: true });
    const timer = setTimeout(abort, budget);
    const context = {
      signal: controller.signal,
      assertCurrent: () => {
        if (controller.signal.aborted || !this.deps.current(call))
          throw new Error('CONTEXT_CHANGED');
      },
    };
    try {
      context.assertCurrent();
      let outcome: UiOutcome;
      try {
        outcome = { status: 'executed', result: await this.deps.execute(call, context) };
      } catch (error) {
        if (controller.signal.aborted) return;
        const message = error instanceof Error ? error.message : '';
        const status = error instanceof Error && 'status' in error ? error.status : undefined;
        const code = DOMAIN.has(message)
          ? message
          : ((typeof status === 'number' ? HTTP_ERRORS.get(status) : undefined) ??
            'UI_EXECUTION_FAILED');
        outcome = {
          status: REJECTED.has(code) ? 'rejected' : 'failed',
          error: { code, message: code },
        };
      }
      await this.deps.answer(call, outcome, controller.signal);
    } finally {
      clearTimeout(timer);
      this.cancellation.signal.removeEventListener('abort', abort);
    }
  }
}
