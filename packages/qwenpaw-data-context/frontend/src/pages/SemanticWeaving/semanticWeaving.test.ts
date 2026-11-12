import assert from 'node:assert/strict';
import {
  SEMANTIC_WEAVING_DEFAULT_PAGE_SIZE,
  SEMANTIC_WEAVING_POLL_INTERVAL_MS,
  SEMANTIC_WEAVING_TASK_STATUSES,
  buildSemanticWeavingSubmitParams,
  buildSemanticWeavingTaskQuery,
  canKillSemanticWeavingTask,
  getSemanticWeavingStatusView,
  getSemanticWeavingSubmitFeedback,
} from './model.ts';

assert.deepEqual(
  buildSemanticWeavingSubmitParams({
    datasource_id: ' mysql-main ',
    task_name: ' full weaving ',
  }),
  {
    datasource_id: 'mysql-main',
    task_name: 'full weaving',
    weave_mode: 'FULL',
  },
);

assert.deepEqual(
  buildSemanticWeavingTaskQuery({
    datasource_name: ' source ',
    task_name: ' task ',
    datasourceName: 'legacy-source',
    taskName: 'legacy-task',
    current: 2,
    pageSize: 50,
  }),
  {
    datasource_name: 'source',
    task_name: 'task',
    page: 2,
    size: 50,
  },
);
assert.deepEqual(
  buildSemanticWeavingTaskQuery({
    datasourceName: 'legacy-source',
    taskName: 'legacy-task',
  }),
  {
    page: 1,
    size: SEMANTIC_WEAVING_DEFAULT_PAGE_SIZE,
  },
);

assert.deepEqual(SEMANTIC_WEAVING_TASK_STATUSES, [
  'QUEUED',
  'RUNNING',
  'SUCCESS',
  'DEGRADED',
  'FAILED',
  'KILLED',
]);
for (const status of SEMANTIC_WEAVING_TASK_STATUSES) {
  assert.deepEqual(getSemanticWeavingStatusView(status), {
    kind: 'known',
    status,
    translationKey: `semanticWeaving.status.${status}`,
  });
}
assert.deepEqual(getSemanticWeavingStatusView('PENDING'), {
  kind: 'protocol-error',
  rawStatus: 'PENDING',
  translationKey: 'semanticWeaving.status.protocolError',
});

assert.equal(canKillSemanticWeavingTask('QUEUED'), true);
assert.equal(canKillSemanticWeavingTask('RUNNING'), true);
assert.equal(canKillSemanticWeavingTask('SUCCESS'), false);
assert.equal(canKillSemanticWeavingTask('DEGRADED'), false);
assert.equal(canKillSemanticWeavingTask('FAILED'), false);
assert.equal(canKillSemanticWeavingTask('KILLED'), false);
assert.equal(canKillSemanticWeavingTask('PENDING'), false);

assert.equal(SEMANTIC_WEAVING_POLL_INTERVAL_MS, 15_000);
assert.deepEqual(getSemanticWeavingSubmitFeedback('SUCCESS'), {
  kind: 'success',
  translationKey: 'semanticWeaving.submitCompleted',
});
assert.deepEqual(getSemanticWeavingSubmitFeedback('DEGRADED'), {
  kind: 'warning',
  translationKey: 'semanticWeaving.submitDegraded',
});
assert.deepEqual(getSemanticWeavingSubmitFeedback('FAILED'), {
  kind: 'error',
  translationKey: 'semanticWeaving.submitFailed',
});
assert.deepEqual(getSemanticWeavingSubmitFeedback('RUNNING'), {
  kind: 'success',
  translationKey: 'semanticWeaving.submitSuccess',
});
assert.deepEqual(getSemanticWeavingSubmitFeedback('PENDING'), {
  kind: 'warning',
  translationKey: 'semanticWeaving.submitUnexpectedStatus',
});
