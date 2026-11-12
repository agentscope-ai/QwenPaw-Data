import assert from 'node:assert/strict';
import test from 'node:test';
import { SessionAccess, hasSessionWriteAccess, isSessionReadOnly } from '../chat/sessionAccess.js';

test('first-send URL resolution keeps write mode while rendered session state is still pending', () => {
  const access = new SessionAccess(1);
  const auth = { ready: true, revision: 1 };
  const modes = [isSessionReadOnly(access, auth, null, null)];
  access.recordCreated('created');
  // createRuntimeSession updates stateRef before setState and URL navigation.
  const resolvedSessionId = 'created';
  modes.push(isSessionReadOnly(access, auth, null, resolvedSessionId));
  // Navigation has committed, but the previous render still had state.session=null.
  modes.push(isSessionReadOnly(access, auth, 'created', resolvedSessionId));
  modes.push(isSessionReadOnly(access, auth, 'created', resolvedSessionId));
  assert.deepEqual(modes, [false, false, false, false]);
  assert.equal(
    new Set(modes.map((readOnly) => `draft:new:0:1:0:${readOnly ? 'read' : 'write'}`)).size,
    1,
  );
});

test('resolved sessions never bypass ownership, navigation or identity restrictions', () => {
  const access = new SessionAccess(1);
  const auth = { ready: true, revision: 1 };
  access.completeCatalog(access.beginCatalog(), ['mine', 'other']);
  assert.equal(isSessionReadOnly(access, auth, 'other', 'mine'), true);
  assert.equal(isSessionReadOnly(access, auth, 'mine', null), true);
  assert.equal(isSessionReadOnly(access, auth, 'shared', 'shared'), true);
  assert.equal(isSessionReadOnly(access, auth, 'mine', 'mine'), false);
  assert.equal(isSessionReadOnly(access, { ...auth, ready: false }, 'mine', 'mine'), true);
  assert.equal(isSessionReadOnly(access, { ...auth, revision: 2 }, 'mine', 'mine'), true);
  access.failCatalog(access.beginCatalog());
  assert.equal(isSessionReadOnly(access, auth, 'mine', 'mine'), true);
});

test('a readable snapshot cannot grant ownership; initial and failed catalogs stay read only', () => {
  const access = new SessionAccess(1);
  assert.equal(access.canWrite('shared'), false);
  const request = access.beginCatalog();
  access.completeCatalog(request, ['mine']);
  assert.equal(access.canWrite('mine'), true);
  assert.equal(access.canWrite('shared'), false);
  const refresh = access.beginCatalog();
  assert.equal(access.canWrite('mine'), true);
  assert.equal(access.canWrite('shared'), false);
  access.failCatalog(refresh);
  assert.equal(access.canWrite('mine'), false);
});

test('background polling preserves confirmed ownership and the runtime mount key', () => {
  const access = new SessionAccess(1);
  access.completeCatalog(access.beginCatalog(), ['mine']);
  const admitted = { authRevision: 1, navigationVersion: 10, sessionEpoch: 3, sessionId: 'mine' };
  const current = { authRevision: 1, navigationVersion: 10, sessionEpoch: 3, ready: true };
  const modes = [access.canWrite('mine')];
  for (let poll = 0; poll < 3; poll += 1) {
    const request = access.beginCatalog();
    modes.push(access.canWrite('mine'));
    assert.equal(hasSessionWriteAccess(access, admitted, current), true);
    access.completeCatalog(request, ['mine']);
    modes.push(access.canWrite('mine'));
  }
  // readOnly participates in AgentScopeRuntimeWebUI's key: no read/write flip.
  assert.deepEqual(modes, Array(7).fill(true));
});

test('a completed refresh immediately revokes sessions missing from the catalog', () => {
  const access = new SessionAccess(1);
  access.completeCatalog(access.beginCatalog(), ['mine']);
  const request = access.beginCatalog();
  assert.equal(access.canWrite('mine'), true);
  access.completeCatalog(request, ['other']);
  assert.equal(access.canWrite('mine'), false);
  assert.equal(access.canWrite('other'), true);
});

test('retry after a failed refresh must reconfirm ownership before allowing writes', () => {
  const access = new SessionAccess(1);
  access.completeCatalog(access.beginCatalog(), ['mine']);
  access.failCatalog(access.beginCatalog());
  const retry = access.beginCatalog();
  assert.equal(access.canWrite('mine'), false);
  access.completeCatalog(retry, ['mine']);
  assert.equal(access.canWrite('mine'), true);
});

test('creation remains writable when an older complete catalog arrives, but deletion removes its proof', () => {
  const access = new SessionAccess(1);
  const request = access.beginCatalog();
  assert.equal(access.canWrite(null), true);
  access.recordCreated('just-created');
  access.completeCatalog(request, ['older-session']);
  assert.equal(access.canWrite('just-created'), true);
  access.forget('just-created');
  assert.equal(access.canWrite('just-created'), false);
  access.forget('older-session');
  assert.equal(access.canWrite('older-session'), false);
});

test('late successes and failures cannot replace a newer catalog result', () => {
  const access = new SessionAccess(1);
  const first = access.beginCatalog();
  const second = access.beginCatalog();
  access.completeCatalog(second, ['current']);
  assert.equal(access.completeCatalog(first, ['stale']), false);
  assert.equal(access.failCatalog(first), false);
  assert.equal(access.canWrite('stale'), false);
  assert.equal(access.canWrite('current'), true);
});

test('identity transitions and A-B-A invalidate admitted writes even when the session ID is unchanged', () => {
  const access = new SessionAccess(1);
  access.completeCatalog(access.beginCatalog(), ['A', 'B']);
  const admitted = { authRevision: 1, navigationVersion: 10, sessionEpoch: 3, sessionId: 'A' };
  const current = { authRevision: 1, navigationVersion: 10, sessionEpoch: 3, ready: true };
  assert.equal(hasSessionWriteAccess(access, admitted, current), true);
  assert.equal(hasSessionWriteAccess(access, admitted, { ...current, ready: false }), false);
  assert.equal(
    hasSessionWriteAccess(access, admitted, { ...current, navigationVersion: 12 }),
    false,
  );
  assert.equal(hasSessionWriteAccess(access, admitted, { ...current, sessionEpoch: 4 }), false);
  // A new identity can have the same user ID and session ID, but must obtain its own proof.
  const switched = new SessionAccess(2);
  switched.completeCatalog(switched.beginCatalog(), ['A']);
  assert.equal(hasSessionWriteAccess(switched, admitted, { ...current, authRevision: 2 }), false);
  assert.equal(hasSessionWriteAccess(access, { ...admitted, sessionId: 'shared' }, current), false);
});

test('later catalog pages extend ownership without replacing earlier pages', () => {
  const access = new SessionAccess(1);
  access.completeCatalog(access.beginCatalog(), ['first-page']);
  assert.equal(access.extendCatalog(['second-page']), true);
  assert.equal(access.canWrite('first-page'), true);
  assert.equal(access.canWrite('second-page'), true);
});

test('targeted identity-scoped lookup confirms a deep-linked owned session', () => {
  const access = new SessionAccess(1);
  access.confirmOwned('deep-link');
  assert.equal(access.canWrite('deep-link'), true);
  assert.equal(access.canWrite('shared'), false);
});
