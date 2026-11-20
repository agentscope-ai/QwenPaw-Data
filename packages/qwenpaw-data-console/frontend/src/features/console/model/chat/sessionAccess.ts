/** Ownership evidence must never be populated from a readable session snapshot. */
export class SessionAccess {
  readonly revision: number;
  status: 'loading' | 'ready' | 'error' = 'loading';
  private request = 0;
  private owners = new Set<string>();
  private created = new Set<string>();

  constructor(revision: number) {
    this.revision = revision;
  }

  beginCatalog() {
    // A background refresh is not a permission change. Keep confirmed ownership
    // until the new result arrives; initial loads and retries still fail closed.
    if (this.status !== 'ready') this.status = 'loading';
    return ++this.request;
  }

  completeCatalog(request: number, ids: readonly string[]) {
    if (request !== this.request) return false;
    this.owners = new Set(ids);
    this.status = 'ready';
    return true;
  }

  extendCatalog(ids: readonly string[]) {
    if (this.status !== 'ready') return false;
    ids.forEach((id) => this.owners.add(id));
    return true;
  }

  /** A targeted lookup through the identity-scoped catalog proves ownership. */
  confirmOwned(id: string) {
    this.created.add(id);
  }

  failCatalog(request: number) {
    if (request !== this.request) return false;
    this.status = 'error';
    return true;
  }

  recordCreated(id: string) {
    this.created.add(id);
  }
  forget(id: string) {
    this.created.delete(id);
    this.owners.delete(id);
  }

  canWrite(id: string | null) {
    return id === null || this.created.has(id) || (this.status === 'ready' && this.owners.has(id));
  }
}

export interface SessionWriteContext {
  authRevision: number; // Identity generation when the operation was admitted.
  navigationVersion: number; // Changes on navigation, including A-B-A.
  sessionEpoch: number; // Also invalidates new drafts created without changing the route.
  sessionId: string | null; // Target session; null is a new draft.
}

/** Use the synchronously resolved session, including before React commits its state. */
export function isSessionReadOnly(
  access: SessionAccess,
  auth: { ready: boolean; revision: number },
  requestedSessionId: string | null | undefined,
  resolvedSessionId: string | null,
) {
  return (
    !auth.ready ||
    access.revision !== auth.revision ||
    !access.canWrite(requestedSessionId ?? resolvedSessionId) ||
    Boolean(requestedSessionId && resolvedSessionId !== requestedSessionId)
  );
}

export function hasSessionWriteAccess(
  access: SessionAccess,
  context: SessionWriteContext,
  current: {
    authRevision: number;
    navigationVersion: number;
    sessionEpoch: number;
    ready: boolean;
  },
) {
  return (
    current.ready &&
    access.revision === current.authRevision &&
    context.authRevision === current.authRevision &&
    context.navigationVersion === current.navigationVersion &&
    context.sessionEpoch === current.sessionEpoch &&
    access.canWrite(context.sessionId)
  );
}
