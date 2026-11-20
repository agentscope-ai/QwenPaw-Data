import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import ts from 'typescript';

// Execute the component's real callbacks without mounting its design system.
function source(path: string) {
  const url = new URL(`../pages/DatasetManagement/${path}`, import.meta.url);
  return ts.createSourceFile(
    path,
    readFileSync(url, 'utf8'),
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  );
}

function evaluate(text: string, context: Record<string, unknown>) {
  const code = ts.transpileModule(`(${text})`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022 },
  }).outputText;
  return vm.runInNewContext(code, context);
}

test('canceling a new dataset releases the same draft lock set by manual form changes', () => {
  const file = source('components/CreateOrEditDataMetaDrawer.tsx');
  const locks = new Set<string>();
  const context = {
    getUiBridge: () => ({
      dirty: (key: string, value: boolean) => (value ? locks.add(key) : locks.delete(key)),
    }),
    visible: true,
    record: undefined,
    canWrite: true,
    manualEdit: { current: false },
    dirty: { current: false },
  };
  const cleanups: (() => void)[] = [];
  let change: (() => void) | undefined;
  function visit(node: ts.Node) {
    if (ts.isCallExpression(node) && node.expression.getText(file) === 'useEffect') {
      const callback = node.arguments[0].getText(file);
      if (callback.includes('dirty(')) {
        const cleanup = evaluate(callback, context)();
        if (cleanup) cleanups.push(cleanup);
      }
    }
    if (ts.isJsxAttribute(node) && node.name.getText(file) === 'onValuesChange') {
      const initializer = node.initializer as ts.JsxExpression;
      change = evaluate(initializer.expression!.getText(file), context);
    }
    ts.forEachChild(node, visit);
  }
  visit(file);
  assert.ok(change);
  change();
  assert.equal(locks.has('dataset.editor'), true);
  cleanups.forEach((cleanup) => cleanup());
  assert.equal(locks.size, 0);
});

test('targeted list reload fetches current data, removes deleted rows and preserves access errors', async () => {
  const file = source('index.tsx');
  let request: string | undefined;
  function visit(node: ts.Node) {
    if (ts.isJsxAttribute(node) && node.name.getText(file) === 'request') {
      request = (node.initializer as ts.JsxExpression).expression!.getText(file);
    }
    ts.forEachChild(node, visit);
  }
  visit(file);
  assert.ok(request);
  let calls = 0;
  let failure: number | undefined;
  const load = evaluate(request, {
    agentTable: { target: { dataset_id: 7, description: 'stale' } },
    getDatasetMeta: async (id: number) => {
      assert.equal(id, 7);
      calls++;
      if (failure) throw Object.assign(new Error('request failed'), { status: failure });
      return { record: { dataset_id: id, description: `revision ${calls}` } };
    },
    Error,
  });
  assert.equal((await load({})).data[0].description, 'revision 1');
  assert.equal((await load({})).data[0].description, 'revision 2');
  failure = 404;
  assert.equal((await load({})).total, 0);
  failure = 403;
  await assert.rejects(load({}), /request failed/);
});
