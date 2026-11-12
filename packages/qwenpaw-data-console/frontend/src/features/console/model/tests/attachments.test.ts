import assert from 'node:assert/strict';
import test from 'node:test';
import {
  ATTACHMENT_EXTENSIONS,
  attachmentExtension,
  validateAttachmentFile,
} from '../chat/attachments.js';

function attachment(name: string, size = 1): File {
  return { name, size } as File;
}

test('支持压缩包、SQL 和 HTML 附件格式', () => {
  ['zip', 'sql', 'gz', 'html'].forEach((extension) => {
    assert.equal(ATTACHMENT_EXTENSIONS.has(extension), true);
    assert.equal(validateAttachmentFile(attachment(`sample.${extension}`), []), null);
  });
});

test('附件扩展名校验忽略大小写', () => {
  assert.equal(attachmentExtension('REPORT.HTML'), 'html');
  assert.equal(validateAttachmentFile(attachment('REPORT.HTML'), []), null);
});

test('已有同名附件时拒绝再次添加', () => {
  assert.equal(validateAttachmentFile(attachment('query.sql'), ['query.sql']), 'duplicate');
});
