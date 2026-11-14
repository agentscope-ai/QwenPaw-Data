import assert from 'node:assert/strict';
import test from 'node:test';
import {
  bootstrapHostAuth,
  getHostAuthContext,
  getHostCurrentUser,
  subscribeHostAuth,
  switchHostWorkspace,
  logoutHostSession,
} from '../../../../services/auth.js';

async function withMockedFetch(mock: typeof fetch, run: () => Promise<void>) {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = mock;
  try {
    await run();
  } finally {
    globalThis.fetch = originalFetch;
  }
}

test('workspace switching and logout invalidate permission observers before the HTTP request finishes', async () => {
  const revisions: number[] = [];
  const unsubscribe = subscribeHostAuth(() => revisions.push(getHostAuthContext().revision));
  try {
    await withMockedFetch(
      async (input) => {
        if (String(input).endsWith('/status')) return Response.json({ enabled: false });
        assert.equal(getHostAuthContext().ready, false);
        assert.equal(getHostCurrentUser(), null);
        return Response.json({});
      },
      async () => {
        await bootstrapHostAuth();
        assert.equal(getHostAuthContext().ready, true);
        const beforeSwitch = getHostAuthContext().revision;
        const switching = switchHostWorkspace('another-workspace');
        assert.equal(getHostAuthContext().revision, beforeSwitch + 1);
        assert.equal(getHostAuthContext().ready, false);
        await switching;
        // Successful workspace switching waits for the caller's normal full reload.
        assert.equal(getHostAuthContext().ready, false);
        await bootstrapHostAuth();
        const beforeLogout = getHostAuthContext().revision;
        const logout = logoutHostSession();
        assert.equal(getHostAuthContext().revision, beforeLogout + 1);
        assert.equal(getHostAuthContext().ready, false);
        await logout;
      },
    );
    assert.ok(revisions.length >= 4);
  } finally {
    unsubscribe();
  }
});

test('a failed switch reconfirms identity instead of restoring cached ownership', async () => {
  const calls: string[] = [];
  await withMockedFetch(
    async (input) => {
      const url = String(input);
      calls.push(url);
      if (url.endsWith('/switch-workspace'))
        return Response.json({ detail: 'denied' }, { status: 403 });
      if (url.endsWith('/status')) return Response.json({ enabled: true });
      return Response.json({
        user_id: 'same-user',
        tenant_id: 'tenant',
        active_workspace_id: 'confirmed',
      });
    },
    async () => {
      await assert.rejects(switchHostWorkspace('denied-workspace'), /denied/);
      assert.equal(getHostCurrentUser()?.active_workspace_id, 'confirmed');
      assert.equal(getHostAuthContext().ready, true);
    },
  );
  assert.deepEqual(calls, ['/api/auth/switch-workspace', '/api/v1/auth/status', '/api/auth/me']);
});
