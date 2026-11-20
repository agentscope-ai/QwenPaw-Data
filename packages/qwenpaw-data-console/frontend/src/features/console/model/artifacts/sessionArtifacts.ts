import type { Artifact, SessionSegmentArtifact } from '../../api/types.js';
import type { Artifact as DisplayArtifact } from '../../types.js';

export interface ArtifactScope {
  sessionId: string;
  revision: number;
  epoch: number;
}

export interface SessionArtifactsState {
  scope: ArtifactScope | null;
  artifacts: Artifact[];
  segmentArtifacts: SessionSegmentArtifact[];
  loading: boolean;
  hasLoaded: boolean;
  error: boolean;
  unavailable: boolean;
}

export const emptySessionArtifacts = (): SessionArtifactsState => ({
  scope: null,
  artifacts: [],
  segmentArtifacts: [],
  loading: false,
  hasLoaded: false,
  error: false,
  unavailable: false,
});

function isArtifact(value: unknown, sessionId: string): value is Artifact {
  if (!value || typeof value !== 'object') return false;
  const item = value as Record<string, unknown>;
  return (
    item.session_id === sessionId &&
    typeof item.id === 'string' &&
    !!item.id &&
    typeof item.path === 'string' &&
    !!item.path &&
    typeof item.name === 'string' &&
    (item.chat_id === null || typeof item.chat_id === 'string') &&
    typeof item.created_at === 'string' &&
    Number.isFinite(Date.parse(item.created_at)) &&
    typeof item.updated_at === 'string' &&
    Number.isFinite(Date.parse(item.updated_at))
  );
}

function isBusinessArtifact(value: unknown): value is SessionSegmentArtifact {
  if (!value || typeof value !== 'object') return false;
  const item = value as Record<string, unknown>;
  return (
    typeof item.name === 'string' &&
    !!item.name &&
    typeof item.description === 'string' &&
    typeof item.relative_path === 'string' &&
    !!item.relative_path &&
    typeof item.chat_id === 'string' &&
    !!item.chat_id &&
    Array.isArray(item.segments) &&
    item.segments.length > 0 &&
    item.segments.every((ref: unknown) => {
      if (!ref || typeof ref !== 'object') return false;
      const segment = ref as Record<string, unknown>;
      return (
        typeof segment.segment_id === 'string' &&
        !!segment.segment_id &&
        typeof segment.chat_id === 'string' &&
        !!segment.chat_id &&
        typeof segment.title === 'string'
      );
    })
  );
}

export function toSessionBusinessArtifact(item: SessionSegmentArtifact): DisplayArtifact {
  return {
    id: item.relative_path,
    name: item.name,
    title: item.description,
    type: item.name.split('.').pop()?.toUpperCase() || 'FILE',
    meta: item.relative_path,
    path: item.relative_path,
    access: { relativePath: item.relative_path },
    segments: item.segments.map((ref) => ({
      id: JSON.stringify([ref.chat_id, ref.segment_id]),
      title: ref.title,
    })),
  };
}

export function filterSessionArtifacts(items: DisplayArtifact[], query: string): DisplayArtifact[] {
  const keyword = query.trim().toLowerCase();
  if (!keyword) return items;
  return items.filter((item) =>
    [item.name, item.path, item.title].some((value) => value?.toLowerCase().includes(keyword)),
  );
}

// Also used at the API boundary: malformed success responses are never empty lists.
export function parseSessionArtifacts(
  value: unknown,
  sessionId: string,
): { artifacts: Artifact[]; segment_artifacts: SessionSegmentArtifact[] } {
  const artifacts =
    value && typeof value === 'object' ? (value as Record<string, unknown>).artifacts : undefined;
  if (!Array.isArray(artifacts) || !artifacts.every((item) => isArtifact(item, sessionId))) {
    throw new Error('Invalid session artifacts response');
  }
  const business = (value as Record<string, unknown>).segment_artifacts;
  if (!Array.isArray(business) || !business.every(isBusinessArtifact)) {
    throw new Error('Invalid session business artifacts response');
  }
  return { artifacts, segment_artifacts: business };
}

export function sameArtifactScope(left: ArtifactScope | null, right: ArtifactScope | null) {
  return (
    left === right ||
    (!!left &&
      !!right &&
      left.sessionId === right.sessionId &&
      left.revision === right.revision &&
      left.epoch === right.epoch)
  );
}

export function createSessionArtifactsController(
  request: (sessionId: string, signal: AbortSignal) => Promise<unknown>,
  notify: (state: SessionArtifactsState) => void,
) {
  let state = emptySessionArtifacts();
  let generation = 0;
  let active: { abort: AbortController; done: Promise<void> } | null = null;
  let trailing = false;
  const publish = (next: SessionArtifactsState) => {
    state = next;
    notify(state);
  };
  const merge = (incoming: Artifact[], preferIncomingOnTie: boolean) => {
    const byPath = new Map(state.artifacts.map((item) => [item.path, item]));
    for (const item of incoming) {
      const previous = byPath.get(item.path);
      const difference = previous
        ? Date.parse(item.updated_at) - Date.parse(previous.updated_at)
        : 1;
      if (!previous || difference > 0 || (difference === 0 && preferIncomingOnTie)) {
        byPath.set(item.path, item);
      }
    }
    return [...byPath.values()].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at));
  };
  const refresh = (queueIfLoading = false): Promise<void> => {
    if (!state.scope) return Promise.resolve();
    if (active) {
      trailing ||= queueIfLoading;
      return active.done;
    }
    const scope = state.scope;
    const token = ++generation;
    const abort = new AbortController();
    const current = () =>
      !abort.signal.aborted && generation === token && sameArtifactScope(state.scope, scope);
    publish({ ...state, loading: true, error: false });
    // Defer request invocation so synchronous throws share the rejection path.
    const done = Promise.resolve()
      .then(() => request(scope.sessionId, abort.signal))
      .then((value) => {
        if (!current()) return;
        const { artifacts, segment_artifacts } = parseSessionArtifacts(value, scope.sessionId);
        publish({
          ...state,
          artifacts: merge(artifacts, false),
          segmentArtifacts: segment_artifacts,
          hasLoaded: true,
          unavailable: false,
          error: false,
        });
      })
      .catch((cause: unknown) => {
        if (!current()) return;
        const status =
          cause && typeof cause === 'object' && 'status' in cause ? cause.status : null;
        const unavailable = status === 401 || status === 403 || status === 404;
        publish({
          ...state,
          error: true,
          unavailable: state.unavailable || unavailable,
          ...(unavailable ? { artifacts: [], segmentArtifacts: [], hasLoaded: false } : {}),
        });
      })
      .finally(() => {
        if (!current()) return;
        active = null;
        publish({ ...state, loading: false });
        const again = trailing;
        trailing = false;
        if (again && !state.unavailable) void refresh();
      });
    active = { abort, done };
    return done;
  };
  const ensureScope = (scope: ArtifactScope | null) => {
    if (sameArtifactScope(state.scope, scope)) return;
    generation++;
    active?.abort.abort();
    active = null;
    trailing = false;
    publish({ ...emptySessionArtifacts(), scope });
    if (scope) void refresh();
  };
  return {
    getSnapshot: () => state,
    ensureScope,
    refresh,
    segmentChanged() {
      if (!state.scope || state.unavailable) return;
      void refresh(true);
    },
    register(artifact: Artifact) {
      if (!state.scope || state.unavailable || !isArtifact(artifact, state.scope.sessionId)) return;
      publish({ ...state, artifacts: merge([artifact], true) });
    },
    dispose() {
      ensureScope(null);
    },
  };
}
