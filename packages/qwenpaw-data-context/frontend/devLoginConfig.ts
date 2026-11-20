import { readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import path from 'node:path';
import { parseEnv } from 'node:util';
import { loadEnv } from 'vite';

type Environment = Record<string, string | undefined>;
type Workspace = { key: string; name: string };
type Accounts = {
  tenants: {
    key: string;
    name: string;
    default_workspace: Workspace;
    extra_workspaces?: Workspace[];
  }[];
  users: { username: string; role: string; tenant: string; workspace: string | null }[];
};
type SetupConfig = { accounts?: Accounts };

function expandHome(value: string): string {
  return value === '~'
    ? homedir()
    : value.startsWith('~/')
      ? path.join(homedir(), value.slice(2))
      : value;
}

export function frontendEnvironment(mode: string, repoRoot: string, processEnv: Environment) {
  const configured = processEnv.QWENPAW_DATA_ENV_FILE;
  const envFile = configured ? path.resolve(expandHome(configured)) : path.join(repoRoot, '.env');
  // An explicit dotenv replaces the root dotenv, just as the shell entrypoints do.
  const env: Environment = configured
    ? { ...processEnv, ...parseEnv(readFileSync(envFile, 'utf8')) }
    : { ...loadEnv(mode, repoRoot, ''), ...processEnv };
  return { env, envFile };
}

export function devLoginConfig(
  command: string,
  repoRoot: string,
  env: Environment,
  envFile: string,
) {
  const empty = {
    accounts: [] as { username: string; role: string; scope: string }[],
    password: '',
    overrides: '',
    accountsFile: '',
  };
  if (command !== 'serve' || (env.QWENPAW_DATA_ENV || 'development') !== 'development') return empty;
  const setupFile = `${envFile}.chat-setup.json`;
  let setup: SetupConfig = {};
  try {
    const value: unknown = JSON.parse(readFileSync(setupFile, 'utf8'));
    if (value && typeof value === 'object' && !Array.isArray(value)) setup = value as SetupConfig;
  } catch {
    // Chat Setup validates its optional config when it runs.
  }
  const accountsFile = setup.accounts
    ? setupFile
    : path.join(repoRoot, 'scripts/chat_setup/fixtures/config.example.json');
  let matrix = setup.accounts;
  try {
    matrix ??= (JSON.parse(readFileSync(accountsFile, 'utf8')) as SetupConfig).accounts;
  } catch {
    throw new Error(`Cannot read chat setup accounts: ${accountsFile}`);
  }
  if (!Array.isArray(matrix?.tenants) || !Array.isArray(matrix.users)) {
    throw new Error(`Invalid chat setup accounts: ${accountsFile}`);
  }
  const accounts = matrix.users.map((user) => {
    const tenant = matrix.tenants.find((item) => item.key === user.tenant);
    const workspace =
      tenant &&
      [tenant.default_workspace, ...(tenant.extra_workspaces || [])].find(
        (item) => item.key === user.workspace,
      );
    if (!tenant || !user.username || !user.role || (user.workspace && !workspace)) {
      throw new Error(`Invalid user or organization reference in ${accountsFile}`);
    }
    return {
      username: user.username,
      role: user.role,
      scope: workspace ? `${tenant.name} / ${workspace.name}` : tenant.name,
    };
  });
  return {
    accounts,
    password: env.VITE_DEV_LOGIN_PASSWORD || '',
    overrides: env.VITE_DEV_LOGIN_PASSWORD_OVERRIDES || '',
    accountsFile,
  };
}
