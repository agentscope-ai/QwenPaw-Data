import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { devLoginConfig, frontendEnvironment } from '../../devLoginConfig.ts';

test('selected dotenv and inline accounts drive login shortcuts', () => {
  const root = mkdtempSync(path.join(tmpdir(), 'dev-login-'));
  try {
    mkdirSync(path.join(root, 'selected'));
    const envFile = path.join(root, 'selected/custom.env');
    const configFile = `${envFile}.chat-setup.json`;
    writeFileSync(path.join(root, '.env'), 'VITE_DEV_LOGIN_PASSWORD=wrong-root-password\n');
    writeFileSync(envFile, "VITE_DEV_LOGIN_PASSWORD='selected-$-password'\n");
    writeFileSync(
      configFile,
      JSON.stringify({
        accounts: {
          tenants: [
            {
              key: 'team',
              name: 'Custom Team',
              default_workspace: { key: 'space', name: 'Custom Space' },
            },
          ],
          users: [
            { username: 'custom-admin', tenant: 'team', role: 'tenant_admin', workspace: null },
            { username: 'custom-user', tenant: 'team', role: 'member', workspace: 'space' },
          ],
        },
        models: { host: { api_key: 'private-model-key' } },
      }),
    );
    const { env } = frontendEnvironment('development', root, {
      QWENPAW_DATA_ENV_FILE: envFile,
      VITE_DEV_LOGIN_PASSWORD: 'stale-process-value',
    });
    const result = devLoginConfig('serve', root, env, envFile);
    assert.equal(result.accountsFile, configFile);
    assert.equal(result.password, 'selected-$-password');
    assert.deepEqual(result.accounts, [
      { username: 'custom-admin', role: 'tenant_admin', scope: 'Custom Team' },
      { username: 'custom-user', role: 'member', scope: 'Custom Team / Custom Space' },
    ]);
    assert.equal(JSON.stringify(result).includes('private-model-key'), false);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('production excludes account matrix and all shortcut secrets without reading the file', () => {
  const env = {
    VITE_DEV_LOGIN_PASSWORD: 'dev-secret',
    VITE_DEV_LOGIN_PASSWORD_OVERRIDES: 'user:override-secret',
  };
  const empty = { accounts: [], password: '', overrides: '', accountsFile: '' };
  assert.deepEqual(devLoginConfig('build', '/unused', env, '/unused/.env'), empty);
  assert.deepEqual(
    devLoginConfig('serve', '/unused', { ...env, QWENPAW_DATA_ENV: 'production' }, '/unused/.env'),
    empty,
  );
});
