import { PAGE_PATHS, validateArguments } from './contracts.js';
import type {
  ObjectRef,
  ToolName,
  UiAction,
  UiArguments,
  UiElement,
  UiResult,
} from './contracts.js';

export interface ExecutionContext {
  signal: AbortSignal; // Cancels on deadline, identity or session change.
  assertCurrent: () => void; // Must run after every await, before a mutation.
}
type Handler = (action: UiAction, context: ExecutionContext) => Promise<Partial<UiResult>>;
type ObjectHandler = (ref: ObjectRef, context: ExecutionContext) => Promise<Partial<UiResult>>;

/** Registry holds only mounted, permission-checked handlers, never discovers DOM controls. */
export class UiRegistry {
  private revision = 0;
  private userRevision = 0;
  private listeners = new Set<() => void>();
  private handlers = new Map<string, { element: UiElement; handler: Handler }>();
  private objects = new Map<string, ObjectHandler>();
  private drafts = new Set<string>();
  private path = '';
  private readyPath = '';
  navigate: (path: string) => void = () => {
    throw new Error('MODULE_NOT_READY');
  };
  authorize: (page: string) => void = () => {
    throw new Error('FORBIDDEN');
  };

  touch = () => {
    this.revision++;
    this.listeners.forEach((listener) => listener());
  };
  userChanged = () => {
    this.userRevision++;
    this.touch();
  };
  setPath(path: string) {
    if (path !== this.path) {
      this.path = path;
      if (this.readyPath !== path) this.readyPath = '';
      this.touch();
    }
  }
  ready = (path: string) => {
    this.readyPath = path;
    this.touch();
  };
  dirty = (key: string, value: boolean) => {
    if (value) this.drafts.add(key);
    else this.drafts.delete(key);
    this.touch();
  };
  assertClean() {
    if (this.drafts.size) throw new Error('USER_EDIT_CONFLICT');
  }
  register = (element: UiElement, handler: Handler) => {
    const entry = { element, handler };
    this.handlers.set(element.element_id, entry);
    this.touch();
    return () => {
      if (this.handlers.get(element.element_id) === entry) {
        this.handlers.delete(element.element_id);
        this.touch();
      }
    };
  };
  object = (kind: string, handler: ObjectHandler) => {
    this.objects.set(kind, handler);
    this.listeners.forEach((listener) => listener());
    return () => {
      if (this.objects.get(kind) === handler) {
        this.objects.delete(kind);
        this.listeners.forEach((listener) => listener());
      }
    };
  };
  wait = (predicate: () => boolean, signal: AbortSignal): Promise<void> => {
    if (signal.aborted) return Promise.reject(new Error('CONTEXT_CHANGED'));
    if (predicate()) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const cleanup = () => {
        this.listeners.delete(check);
        signal.removeEventListener('abort', abort);
      };
      const check = () => {
        if (predicate()) {
          cleanup();
          resolve();
        }
      };
      const abort = () => {
        cleanup();
        reject(new Error('CONTEXT_CHANGED'));
      };
      this.listeners.add(check);
      signal.addEventListener('abort', abort, { once: true });
      check();
    });
  };
  snapshot(): UiResult {
    const page = Object.entries(PAGE_PATHS).find(
      ([, path]) =>
        path === this.path ||
        (path.includes(':datasourceId') && this.path.startsWith('/data-profiling/')),
    )?.[0] as keyof typeof PAGE_PATHS | undefined;
    return {
      page_id: page ?? 'manual_only',
      view_revision: String(this.revision),
      saved: false,
      completed_steps: [],
      elements: [...this.handlers.values()].map(({ element }) => element),
    };
  }
  async execute(name: ToolName, raw: UiArguments, context: ExecutionContext): Promise<UiResult> {
    const original = context;
    const userRevision = this.userRevision;
    context = {
      signal: original.signal,
      assertCurrent: () => {
        original.assertCurrent();
        if (userRevision !== this.userRevision) throw new Error('VIEW_STALE');
      },
    };
    const args = validateArguments(name, raw);
    context.assertCurrent();
    if (name === 'ui_inspect') return this.snapshot();
    let result: Partial<UiResult>;
    if (name === 'ui_act') {
      if (args.expected_view_revision !== String(this.revision)) throw new Error('VIEW_STALE');
      const entry = this.handlers.get(args.element_id!);
      if (!entry || !entry.element.allowed_actions.includes(args.action!.type))
        throw new Error('ACTION_NOT_ALLOWED');
      result = await entry.handler(args.action!, context);
    } else {
      this.assertClean();
      const ref = args.object_ref;
      const page =
        name === 'ui_open_page'
          ? args.page_id!
          : ref!.kind === 'cm.dataset'
            ? 'datasets'
            : 'artifact';
      this.authorize(page);
      const path =
        page === 'artifact'
          ? '/console'
          : PAGE_PATHS[page as keyof typeof PAGE_PATHS].replace(
              ':datasourceId',
              encodeURIComponent(args.params?.datasourceId ?? ''),
            );
      if (this.path !== path) this.navigate(path);
      if (ref) {
        await this.wait(() => this.objects.has(ref.kind) && this.path === path, context.signal);
        context.assertCurrent();
        result = await this.objects.get(ref.kind)!(ref, context);
      } else {
        await this.wait(() => this.readyPath === path && this.path === path, context.signal);
        context.assertCurrent();
        result = { page_id: args.page_id, view_state: 'opened', completed_steps: ['opened'] };
      }
    }
    context.assertCurrent();
    return { ...this.snapshot(), ...result, view_revision: String(this.revision), saved: false };
  }
}
export const uiRegistry = new UiRegistry();
