import { readFile, readdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { Linter } from 'eslint';
import globals from 'globals';

// Check emitted modules, where external re-exports can lose their bindings even
// though the original TypeScript passed. This is not a browser/runtime test.
const root = fileURLToPath(new URL('../dist-embedded/', import.meta.url));
const allowedGlobals = {
  ...globals.browser,
  ...globals.node,
  ...globals.es2025,
  // tslib emits a typeof-guarded fallback for this newer built-in.
  SuppressedError: 'readonly',
};
const linter = new Linter();
const config = [
  {
    linterOptions: { noInlineConfig: true, reportUnusedDisableDirectives: false },
    languageOptions: { ecmaVersion: 'latest', sourceType: 'module' },
    plugins: {
      emitted: {
        rules: {
          'module-bindings': {
            meta: { schema: [] },
            create(context) {
              return {
                'Program:exit'() {
                  const scope = context.sourceCode.scopeManager.scopes.find(
                    (item) => item.type === 'module',
                  );
                  for (const ref of scope.through) {
                    if (ref.from.variableScope !== scope) continue;
                    if (Object.hasOwn(allowedGlobals, ref.identifier.name)) continue;
                    context.report({
                      node: ref.identifier,
                      message: `Unbound module-level reference: ${ref.identifier.name}`,
                    });
                  }
                },
              };
            },
          },
        },
      },
    },
    rules: { 'emitted/module-bindings': 'error' },
  },
];

let failures = 0;
const files = (await readdir(root, { recursive: true })).filter((file) => file.endsWith('.js'));
if (!files.length) throw new Error('Build the embedded library before checking its bindings.');
for (const file of files) {
  const code = await readFile(`${root}/${file}`, 'utf8');
  // These unresolved calls survive the next bundling step and fail in browsers.
  if (/\b(?:__require|require)\(\s*['"](?:react(?:-dom|-router(?:-dom)?)?|antd|@agentscope-ai\/design|@ant-design\/icons)(?:\/[^'"]*)?['"]\s*\)/.test(code)) {
    console.error(`${file}: external CommonJS require remains in browser library`);
    failures += 1;
  }
  const messages = linter.verify(code, config);
  for (const message of messages.filter((item) => item.severity === 2)) {
    console.error(`${file}:${message.line}:${message.column} ${message.message}`);
    failures += 1;
  }
}
if (failures) process.exitCode = 1;
else
  console.log(
    `Embedded bindings: ${files.length} modules checked, no unbound module-level references.`,
  );
