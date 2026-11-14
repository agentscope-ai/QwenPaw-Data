import assert from 'node:assert/strict';
import {
  EMBEDDING_REBUILD_IN_PROGRESS,
  GATEWAY_UNAVAILABLE,
  REBUILD_IN_PROGRESS_MESSAGE,
  SERVICE_UNAVAILABLE_MESSAGE,
  parseApiError,
} from './apiError.ts';

const TENGINE_503 = `<!DOCTYPE HTML PUBLIC "-//IETF//DTD HTML 2.0//EN">
<html>
<head><title>503 Service Temporarily Unavailable</title></head>
<body>
<center><h1>503 Service Temporarily Unavailable</h1></center>
Sorry for the inconvenience.<br/>
<table>
<tr><td>URL:</td><td>https://context.example.com/api/semantic-config/probe/overview?page=1&amp;size=20</td></tr>
</table>
</body>
</html>`;

const ingressError = parseApiError(TENGINE_503, 503, 'Service Temporarily Unavailable');
assert.equal(ingressError.status, 503);
assert.equal(ingressError.code, GATEWAY_UNAVAILABLE);
assert.equal(ingressError.message, SERVICE_UNAVAILABLE_MESSAGE);
assert.equal(ingressError.message.includes('<!DOCTYPE'), false);
assert.equal(ingressError.message.includes('<html'), false);

const rebuildJson = parseApiError(
  {
    detail: {
      error: {
        code: EMBEDDING_REBUILD_IN_PROGRESS,
        message: '图谱正在重建，其他功能暂时不可用，请稍后刷新重试',
        details: { job_id: 'job-1' },
      },
    },
    job_id: 'job-1',
  },
  423,
);
assert.equal(rebuildJson.status, 423);
assert.equal(rebuildJson.code, EMBEDDING_REBUILD_IN_PROGRESS);
assert.equal(rebuildJson.message, REBUILD_IN_PROGRESS_MESSAGE);
assert.deepEqual(rebuildJson.details, { job_id: 'job-1' });

const legacyRebuild = parseApiError(
  { detail: 'Embedding rebuild in progress', job_id: 'job-2' },
  503,
);
assert.equal(legacyRebuild.code, EMBEDDING_REBUILD_IN_PROGRESS);
assert.equal(legacyRebuild.message, REBUILD_IN_PROGRESS_MESSAGE);

const structured = parseApiError(
  {
    timestamp: '2026-08-23T06:00:00Z',
    status: 400,
    error: 'Bad Request',
    message: 'semantic-config-400',
  },
  400,
);
assert.equal(structured.message, 'semantic-config-400');
assert.equal(structured.code, undefined);
