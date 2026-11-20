import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const frontendRoot = process.argv[2];
const lock = JSON.parse(readFileSync(join(frontendRoot, 'package-lock.json'), 'utf8'));

function matches(values, current) {
  if (!values?.length) return true;
  const allowed = values.filter((value) => !value.startsWith('!'));
  return (
    !values.includes(`!${current}`) &&
    (allowed.length === 0 || allowed.includes('any') || allowed.includes(current))
  );
}

const libc =
  process.platform === 'linux'
    ? process.report.getReport().header.glibcVersionRuntime
      ? 'glibc'
      : 'musl'
    : undefined;

for (const [path, entry] of Object.entries(lock.packages)) {
  if (!path) continue;
  // Native optional packages for other platforms are intentionally absent.
  if (entry.optional) {
    if (!entry.os && !entry.cpu && !entry.libc) continue;
    if (
      !matches(entry.os, process.platform) ||
      !matches(entry.cpu, process.arch) ||
      !matches(entry.libc, libc)
    ) {
      continue;
    }
  }
  try {
    const installed = JSON.parse(readFileSync(join(frontendRoot, path, 'package.json'), 'utf8'));
    if (installed.version === entry.version) continue;
  } catch {
    // A missing or unreadable package manifest also requires reinstalling.
  }
  console.error(`Frontend dependency needs reinstalling: ${path}`);
  process.exit(1);
}
