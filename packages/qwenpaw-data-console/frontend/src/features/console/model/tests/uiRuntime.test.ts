import assert from 'node:assert/strict';
import test from 'node:test';
import { UiExecutor } from '../../ui-runtime/executor.js';
import { UiRegistry } from '../../ui-runtime/registry.js';
import {
  readUiCall,
  selectPendingUiCalls,
  validateArguments,
  PAGE_PATHS,
} from '../../ui-runtime/contracts.js';
import type { UiCall } from '../../ui-runtime/contracts.js';

const call: UiCall = {
  session_id: 's',
  chat_id: 'c',
  call_id: 't',
  name: 'ui_inspect',
  arguments: { scope: 'current' },
  expires_at: new Date(Date.now() + 60000).toISOString(),
};
test('duplicate live events execute only once', async () => {
  let calls = 0;
  let answers = 0;
  const executor = new UiExecutor({
    current: () => true,
    execute: async () => {
      calls++;
      return { view_revision: '1', completed_steps: [], saved: false };
    },
    answer: async () => {
      answers++;
    },
  });
  await Promise.all([executor.accept(call), executor.accept(call)]);
  assert.deepEqual([calls, answers], [1, 1]);
});
test('cancellation prevents queued execution', async () => {
  let calls = 0;
  const executor = new UiExecutor({
    current: () => true,
    execute: async () => {
      calls++;
      throw new Error('unexpected');
    },
    answer: async () => undefined,
  });
  executor.cancel();
  await executor.accept({ ...call, call_id: 'new' });
  assert.equal(calls, 0);
});
test('only registered actions run; stale inspection and dirty navigation fail', async () => {
  const registry = new UiRegistry();
  let value = '';
  let calls = 0;
  const context = { signal: new AbortController().signal, assertCurrent: () => undefined };
  registry.register({ element_id: 'description', allowed_actions: ['fill'] }, async (action) => {
    value = String(action.value);
    calls++;
    registry.touch();
    return { value };
  });
  const revision = registry.snapshot().view_revision;
  const args = {
    element_id: 'description',
    expected_view_revision: revision,
    action: { type: 'fill' as const, value: 'draft' },
  };
  assert.equal((await registry.execute('ui_act', args, context)).saved, false);
  await assert.rejects(registry.execute('ui_act', args, context), /VIEW_STALE/);
  await assert.rejects(
    registry.execute(
      'ui_act',
      { ...args, expected_view_revision: registry.snapshot().view_revision, element_id: 'save' },
      context,
    ),
    /ACTION_NOT_ALLOWED/,
  );
  registry.dirty('editor', true);
  await assert.rejects(
    registry.execute('ui_open_page', { page_id: 'datasets', params: {} }, context),
    /USER_EDIT_CONFLICT/,
  );
  assert.equal(calls, 1);
  assert.equal(value, 'draft');
});
test('strict tool arguments and full navigation catalogue', () => {
  assert.equal(Object.keys(PAGE_PATHS).length, 19);
  assert.throws(() =>
    validateArguments('ui_open_page', { page_id: 'datasets', params: { url: '/admin' } }),
  );
  assert.throws(() =>
    validateArguments('ui_act', {
      element_id: 'x',
      expected_view_revision: '1',
      action: { type: 'click', value: 'unexpected' },
    }),
  );
  assert.throws(() =>
    validateArguments('ui_open_object', {
      object_ref: { kind: 'cm.dataset', datasource_id: 'd', dataset_id: true, domain_id: 1 },
    }),
  );
});

test('answer failure is not retried and does not repeat the handler', async () => {
  let executions = 0;
  let uploads = 0;
  const executor = new UiExecutor({
    current: () => true,
    execute: async () => {
      executions++;
      return { view_revision: '1', completed_steps: [], saved: false };
    },
    answer: async () => {
      uploads++;
      throw new Error('network');
    },
  });
  await assert.rejects(() => executor.accept(call), /network/);
  assert.equal(executions, 1);
  assert.equal(uploads, 1);
});
test('HTTP and unexpected execution errors return a safe outcome without waiting for timeout', async () => {
  for (const [httpStatus, status, code] of [
    [404, 'rejected', 'TARGET_NOT_FOUND'],
    [403, 'rejected', 'FORBIDDEN'],
    [500, 'failed', 'UI_EXECUTION_FAILED'],
  ] as const) {
    const outcomes: unknown[] = [];
    const executor = new UiExecutor({
      current: () => true,
      execute: async () => {
        throw Object.assign(new Error('Raw service error'), { status: httpStatus });
      },
      answer: async (_call, outcome) => {
        outcomes.push(outcome);
      },
    });
    await executor.accept(call);
    assert.deepEqual(outcomes, [{ status, error: { code, message: code } }]);
  }
});
test('user interaction during an async action invalidates its remaining mutation', async () => {
  const registry = new UiRegistry();
  let finish!: () => void;
  let mutated = false;
  registry.register(
    { element_id: 'edit', allowed_actions: ['click'] },
    async (_action, context) => {
      await new Promise<void>((resolve) => {
        finish = resolve;
      });
      context.assertCurrent();
      mutated = true;
      return {};
    },
  );
  const task = registry.execute(
    'ui_act',
    {
      element_id: 'edit',
      expected_view_revision: registry.snapshot().view_revision,
      action: { type: 'click' },
    },
    { signal: new AbortController().signal, assertCurrent: () => undefined },
  );
  registry.userChanged();
  finish();
  await assert.rejects(task, /VIEW_STALE/);
  assert.equal(mutated, false);
});

test('child page readiness survives the parent layout effect; old pages do not qualify', async () => {
  const registry = new UiRegistry();
  registry.authorize = () => undefined;
  const context = { signal: new AbortController().signal, assertCurrent: () => undefined };
  registry.ready('/console/settings/agent');
  registry.setPath('/console/settings/agent');
  const result = await registry.execute(
    'ui_open_page',
    { page_id: 'model_settings', params: {} },
    context,
  );
  assert.equal(result.view_state, 'opened');
  registry.setPath('/data-set');
  const controller = new AbortController();
  const waiting = registry.execute(
    'ui_open_page',
    { page_id: 'datasets', params: {} },
    { ...context, signal: controller.signal },
  );
  controller.abort();
  await assert.rejects(waiting, /CONTEXT_CHANGED/);
});

test('completed plugin_call with awaiting_ui becomes a UI call', () => {
  const event = {
    object: 'message',
    type: 'plugin_call',
    status: 'completed',
    chat_id: 'c',
    sequence: 1,
    metadata: { awaiting_ui: true, expires_at: call.expires_at },
    content: [
      {
        type: 'data',
        data: { call_id: 't', name: 'ui_inspect', arguments: '{"scope":"current"}' },
      },
    ],
  };
  assert.deepEqual(readUiCall(event, 's'), call);
  assert.equal(readUiCall({ ...event, metadata: { awaiting_confirmation: true } }, 's'), null);
  assert.throws(
    () =>
      readUiCall(
        {
          ...event,
          content: [{ type: 'data', data: { call_id: 't', name: 'ui_inspect', arguments: '{' } }],
        },
        's',
      ),
    /INVALID_ARGUMENT/,
  );
  const finished = {
    object: 'message' as const,
    type: 'plugin_call_output',
    status: 'completed' as const,
    chat_id: 'c',
    sequence: 2,
    content: [{ type: 'data', data: { call_id: 't', output: 'done' } }],
    metadata: null,
  };
  assert.deepEqual(selectPendingUiCalls([event], { id: 'c', session_id: 's' }), [call]);
  assert.deepEqual(selectPendingUiCalls([event, finished], { id: 'c', session_id: 's' }), []);
});
