import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

for (const directory of ['features/console/model/__test__', 'services/__test__']) {
  // Node's automatic discovery skips node_modules unless it is the cwd.
  const cwd = fileURLToPath(new URL(`../node_modules/.tmp/dataconsole-tests/${directory}/`, import.meta.url));
  const result = spawnSync(process.execPath, ['--test'], { cwd, stdio: 'inherit', env: process.env });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}
