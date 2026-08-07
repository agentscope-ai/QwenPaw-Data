import { writeFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';

const result = spawnSync(
  process.platform === 'win32' ? 'npm.cmd' : 'npm',
  ['sbom', '--omit=dev', '--sbom-format', 'cyclonedx'],
  { encoding: 'utf8', maxBuffer: 20 * 1024 * 1024 },
);

if (result.status !== 0) {
  console.error(result.stderr || 'npm sbom failed');
  process.exit(result.status || 1);
}

const bom = JSON.parse(result.stdout);

function stripUrlCredentials(value) {
  if (typeof value === 'string') {
    return value.replace(/(https?:\/\/)[^/@\s]+@/gi, '$1');
  }
  if (Array.isArray(value)) {
    return value.map(stripUrlCredentials);
  }
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value).map(([key, child]) => [key, stripUrlCredentials(child)]),
    );
  }
  return value;
}

writeFileSync(
  'frontend.cdx.json',
  `${JSON.stringify(stripUrlCredentials(bom), null, 2)}\n`,
  { mode: 0o600 },
);
console.log('Wrote credential-sanitized frontend.cdx.json');
