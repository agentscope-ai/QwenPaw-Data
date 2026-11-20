import assert from 'node:assert/strict';
import test from 'node:test';
import { artifactFromLink } from '../artifacts/artifactLinks.js';
import { getArtifactPreviewKind } from '../artifacts/artifactPreview.js';
import { resolveArtifactAccess } from '../artifacts/artifacts.js';

test('HTML preview keeps the authenticated network URL instead of inheriting CSP via blob', async () => {
  const path = 'artifacts/ses_example/DAU趋势分析报告_v2.html';
  const artifact = artifactFromLink(`#artifact:${encodeURI(path)}`);
  assert.ok(artifact);
  assert.equal(artifact.name, 'DAU趋势分析报告_v2.html');
  assert.equal(getArtifactPreviewKind(artifact), 'html');
  const calls: unknown[][] = [];
  let blobCreated = false;
  let blobRevoked = false;
  const documentUrl =
    'https://console.example/api/sessions/session-1/files/access?path=report.html';
  const access = await resolveArtifactAccess(
    'session-1',
    artifact,
    'preview',
    {
      getWorkspaceFile: async (...args) => {
        calls.push(args);
        const response = new Response('<h1>Report</h1>');
        Object.defineProperty(response, 'url', { value: documentUrl });
        return response;
      },
    },
    {
      createObjectURL: () => {
        blobCreated = true;
        return 'blob:preview';
      },
      revokeObjectURL: () => {
        blobRevoked = true;
      },
    },
  );
  assert.deepEqual(calls, [['session-1', path, 'preview']]);
  assert.equal(access.url, documentUrl);
  access.release();
  assert.equal(blobCreated, false);
  assert.equal(blobRevoked, false);
});

test('downloads and non-HTML previews retain blob lifetime management', async () => {
  for (const [name, purpose] of [
    ['report.html', 'download'],
    ['report.csv', 'preview'],
  ] as const) {
    const artifact = artifactFromLink(`#artifact:${name}`)!;
    let released = '';
    let result: Blob | undefined;
    const access = await resolveArtifactAccess(
      'session-1',
      artifact,
      purpose,
      {
        getWorkspaceFile: async () => new Response('file bytes'),
      },
      {
        createObjectURL: (blob) => {
          result = blob;
          return 'blob:file';
        },
        revokeObjectURL: (url) => {
          released = url;
        },
      },
    );
    assert.equal(await result?.text(), 'file bytes');
    assert.equal(access.url, 'blob:file');
    access.release();
    assert.equal(released, 'blob:file');
  }
});

test('HTML preview cannot silently fall back to a CSP-inheriting blob when URL is missing', async () => {
  await assert.rejects(
    resolveArtifactAccess('session-1', artifactFromLink('#artifact:r.html')!, 'preview', {
      getWorkspaceFile: async () => new Response('<p>report</p>'),
    }),
    /document URL/,
  );
});

test('raw and encoded paths preserve directories and decode only once', () => {
  for (const path of [
    'artifacts/ses_example/result.yaml',
    'artifacts/报告.csv',
    'artifacts/report final (2).html',
    'artifacts/100%25 + growth#1?.csv',
  ]) {
    assert.equal(artifactFromLink(`#artifact:${encodeURI(path)}`)?.access?.relativePath, path);
  }
  assert.equal(artifactFromLink('#artifact:artifacts/报告.csv')?.path, 'artifacts/报告.csv');
});

test('ordinary links, anchors and invalid artifact links do not open previews', () => {
  for (const href of [
    null,
    '',
    '#section',
    'https://example.com/#artifact:report.html',
    '/report.html',
    '#artifact:',
    '#artifact:%20',
    '#artifact:%E0%A4',
  ]) {
    assert.equal(artifactFromLink(href), null);
  }
});

test('HTML preview retains exact comment source lines alongside its network URL', async () => {
  const fixtures = [
    '',
    '<p>single line</p>',
    '<html>\n<head></head>\n<body>\n<p>target</p>\n<script>echarts.init(chart)</script>\n</body>\n</html>\n',
    '<html>\r\n<head></head>\r\n<body>\r\n<p>target</p>\r\n<script>echarts.init(chart)</script>\r\n</body>\r\n</html>\r\n',
    '<p>plain</p>\n<p>target</p>\n',
    '<html><head></head><body><script>echarts.init(chart)</script></body></html>',
  ];
  const artifact = artifactFromLink('#artifact:reports/result.html');
  assert.ok(artifact);
  for (const source of fixtures) {
    const calls: unknown[][] = [];
    const blobs: Blob[] = [];
    const revoked: string[] = [];
    const documentUrl = 'https://console.example/api/sessions/session-html/files/access';
    const access = await resolveArtifactAccess(
      'session-html',
      artifact,
      'preview',
      {
        getWorkspaceFile: async (...args) => {
          calls.push(args);
          const response = new Response(source);
          Object.defineProperty(response, 'url', { value: documentUrl });
          return response;
        },
      },
      {
        createObjectURL: (blob) => {
          blobs.push(blob);
          return 'blob:html';
        },
        revokeObjectURL: (url) => revoked.push(url),
      },
    );
    assert.equal(access.sourceText, source);
    assert.deepEqual(calls, [['session-html', 'reports/result.html', 'preview']]);
    assert.equal(access.url, documentUrl);
    assert.equal(blobs.length, 0);
    assert.doesNotMatch(access.sourceText!, /data-qwenpaw-data-echarts-resize-support/);
    if (source.includes('echarts.init') && source.includes('\n')) {
      assert.equal(access.sourceText!.split('\n')[3].trim(), '<p>target</p>');
    }
    access.release();
    assert.deepEqual(revoked, []);
  }
});

test('HTML downloads and non-HTML previews keep their original payload and access contract', async () => {
  const source = '<script>echarts.init(chart)</script>\r\n';
  for (const [filename, purpose] of [
    ['result.html', 'download'],
    ['result.md', 'preview'],
    ['result.csv', 'preview'],
  ] as const) {
    const artifact = artifactFromLink(`#artifact:reports/${filename}`);
    assert.ok(artifact);
    const blobs: Blob[] = [];
    const calls: unknown[][] = [];
    const access = await resolveArtifactAccess(
      'session-compat',
      artifact,
      purpose,
      {
        getWorkspaceFile: async (...args) => {
          calls.push(args);
          return new Response(source);
        },
      },
      { createObjectURL: (blob) => (blobs.push(blob), 'blob:original'), revokeObjectURL: () => {} },
    );
    assert.equal(access.sourceText, undefined);
    assert.equal(await blobs[0].text(), source);
    assert.deepEqual(calls, [['session-compat', `reports/${filename}`, purpose]]);
  }
});

test('failed HTML access propagates without creating a stale display URL', async () => {
  const artifact = artifactFromLink('#artifact:reports/result.html');
  assert.ok(artifact);
  await assert.rejects(
    resolveArtifactAccess(
      'session-failed',
      artifact,
      'preview',
      {
        getWorkspaceFile: async () => {
          throw new Error('file unavailable');
        },
      },
      { createObjectURL: () => assert.fail('must not create URL'), revokeObjectURL: () => {} },
    ),
    /file unavailable/,
  );
});
