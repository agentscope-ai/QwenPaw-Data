import assert from 'node:assert/strict';
import {
  SEMANTIC_EXCEL_MAX_SIZE_BYTES,
  SEMANTIC_EXCEL_TEMPLATE_OBJECTS,
  downloadExcelTemplate,
  exportSemanticConfigExcel,
  importExcel,
  isSuccessfulExcelImport,
  saveExcelDownload,
  validateSemanticExcelFile,
  type ExcelDownloadAnchor,
  type ExcelDownloadEnvironment,
  type ExcelImportResult,
} from './excelImport.ts';
import { parseContentDispositionFilename } from './request.ts';

interface RequestHarness {
  requestTestCalls: Array<{
    method: string;
    url: string;
    data?: unknown;
  }>;
  resetRequestTestHarness: () => void;
  setRequestTestResponse: (value: unknown) => void;
}

interface BlobRequestHarness {
  blobRequestTestCalls: Array<{ path: string; options?: unknown }>;
  resetBlobRequestTestHarness: () => void;
  setBlobRequestTestResponse: (value: { blob: Blob; filename?: string }) => void;
}

// @ts-expect-error test-only virtual module
const requestHarness = (await import('semantic-test:request')) as RequestHarness;
// @ts-expect-error test-only virtual module
const blobHarness = (await import('semantic-test:blob-request')) as BlobRequestHarness;

assert.equal(
  validateSemanticExcelFile({ name: 'semantic-model.XLSX', size: SEMANTIC_EXCEL_MAX_SIZE_BYTES }),
  undefined,
);
assert.equal(validateSemanticExcelFile({ name: 'semantic-model.xls', size: 1024 }), 'extension');
assert.equal(
  validateSemanticExcelFile({
    name: 'semantic-model.xlsx',
    size: SEMANTIC_EXCEL_MAX_SIZE_BYTES + 1,
  }),
  'size',
);

const successResult: ExcelImportResult = {
  success: true,
  summary: {
    biz_domain: 1,
    dataset: 1,
    dataset_column: 1,
    dimension: 1,
    dataset_dimension: 1,
    metric: 1,
    metric_formula: 1,
  },
  errors: [],
};
assert.equal(isSuccessfulExcelImport(successResult), true);
assert.equal(
  isSuccessfulExcelImport({
    ...successResult,
    success: false,
    errors: [{ sheet: 'dataset', row: 2, message: 'domain missing' }],
  }),
  false,
);
assert.equal(
  isSuccessfulExcelImport({
    ...successResult,
    errors: [{ sheet: 'dataset', row: 2, message: 'must rollback' }],
  }),
  false,
);

requestHarness.resetRequestTestHarness();
requestHarness.setRequestTestResponse(successResult);
const uploadFile = Object.assign(new Blob(['xlsx']), {
  name: 'semantic-model.xlsx',
  lastModified: 0,
}) as File;
await importExcel(uploadFile, 'mysql-main');
const importCall = requestHarness.requestTestCalls.shift();
assert.equal(importCall?.method, 'post');
assert.equal(importCall?.url, '/api/semantic-config/import/excel');
assert.ok(importCall?.data instanceof FormData);
assert.ok(importCall.data.get('file') instanceof Blob);
assert.equal(importCall.data.get('datasource_id'), 'mysql-main');

assert.deepEqual(SEMANTIC_EXCEL_TEMPLATE_OBJECTS, [
  'biz_domain',
  'dataset',
  'dataset_column',
  'dimension',
  'dataset_dimension',
  'metric',
  'metric_formula',
]);

blobHarness.resetBlobRequestTestHarness();
const downloadBlob = new Blob(['xlsx'], {
  type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
});
blobHarness.setBlobRequestTestResponse({ blob: downloadBlob, filename: 'server-export.xlsx' });
const exported = await exportSemanticConfigExcel('mysql/main');
assert.equal(
  blobHarness.blobRequestTestCalls.shift()?.path,
  '/api/semantic-config/export/excel?datasource_id=mysql%2Fmain',
);
assert.equal(exported.filename, 'server-export.xlsx');

blobHarness.setBlobRequestTestResponse({ blob: downloadBlob });
const allTemplate = await downloadExcelTemplate();
assert.equal(
  blobHarness.blobRequestTestCalls.shift()?.path,
  '/api/semantic-config/import/excel/template?object=all',
);
assert.equal(allTemplate.filename, 'semantic-config-template-all.xlsx');

for (const object of SEMANTIC_EXCEL_TEMPLATE_OBJECTS) {
  blobHarness.setBlobRequestTestResponse({ blob: downloadBlob });
  const template = await downloadExcelTemplate(object);
  assert.equal(
    blobHarness.blobRequestTestCalls.shift()?.path,
    `/api/semantic-config/import/excel/template?object=${object}`,
  );
  assert.equal(template.filename, `semantic-config-template-${object}.xlsx`);
}

blobHarness.setBlobRequestTestResponse({ blob: downloadBlob, filename: '../unsafe-name' });
assert.equal((await downloadExcelTemplate('metric')).filename, 'unsafe-name.xlsx');
blobHarness.blobRequestTestCalls.shift();

assert.equal(
  parseContentDispositionFilename("attachment; filename*=UTF-8''semantic%20model.xlsx"),
  'semantic model.xlsx',
);
assert.equal(
  parseContentDispositionFilename('attachment; filename="semantic-model.xlsx"'),
  'semantic-model.xlsx',
);

let appended = false;
let clicked = false;
let removed = false;
let revokedUrl = '';
const anchor: ExcelDownloadAnchor = {
  href: '',
  download: '',
  click: () => {
    clicked = true;
  },
  remove: () => {
    removed = true;
  },
};
const downloadEnvironment: ExcelDownloadEnvironment = {
  createObjectURL: () => 'blob:semantic-excel',
  revokeObjectURL: (url) => {
    revokedUrl = url;
  },
  createAnchor: () => anchor,
  appendAnchor: () => {
    appended = true;
  },
};
saveExcelDownload(exported, downloadEnvironment);
assert.equal(anchor.href, 'blob:semantic-excel');
assert.equal(anchor.download, 'server-export.xlsx');
assert.equal(appended, true);
assert.equal(clicked, true);
assert.equal(removed, true);
assert.equal(revokedUrl, 'blob:semantic-excel');
