import { existsSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { resolve as resolvePath } from 'node:path';

const sourceRoot = resolvePath(process.cwd(), 'src');

const requestModuleSource = `
export const requestTestCalls = [];
let nextResponse;

export function resetRequestTestHarness() {
  requestTestCalls.length = 0;
  nextResponse = undefined;
}

export function setRequestTestResponse(value) {
  nextResponse = value;
}

function record(method, url, data, config) {
  requestTestCalls.push({ method, url, data, config });
  return Promise.resolve(nextResponse);
}

export const request = {
  get(url, config) {
    return record('get', url, undefined, config);
  },
  post(url, data, config) {
    return record('post', url, data, config);
  },
  put(url, data, config) {
    return record('put', url, data, config);
  },
  delete(url, config) {
    return record('delete', url, undefined, config);
  },
};

export default request;
`;

const authModuleSource = `
export function formatDetail(value) {
  return typeof value === 'string' ? value : '';
}
`;

const blobRequestModuleSource = `
export const blobRequestTestCalls = [];
let nextBlobResponse = { blob: new Blob([]) };

export function resetBlobRequestTestHarness() {
  blobRequestTestCalls.length = 0;
  nextBlobResponse = { blob: new Blob([]) };
}

export function setBlobRequestTestResponse(value) {
  nextBlobResponse = value;
}

export function requestBlobDownload(path, options) {
  blobRequestTestCalls.push({ path, options });
  return Promise.resolve(nextBlobResponse);
}

export function requestBlob(path, options) {
  blobRequestTestCalls.push({ path, options });
  return Promise.resolve(nextBlobResponse.blob);
}
`;

function resolveTypeScriptPath(path) {
  const candidates = [path, `${path}.ts`, `${path}.tsx`, resolvePath(path, 'index.ts')];
  return candidates.find((candidate) => existsSync(candidate));
}

export async function resolve(specifier, context, nextResolve) {
  if (specifier === 'semantic-test:request' || specifier === '@/utils/request') {
    return { url: 'semantic-test:request', shortCircuit: true };
  }
  if (specifier === '@/auth') {
    return { url: 'semantic-test:auth', shortCircuit: true };
  }
  if (specifier === 'semantic-test:blob-request' || specifier === '@/services/request') {
    return { url: 'semantic-test:blob-request', shortCircuit: true };
  }
  if (specifier.startsWith('@/')) {
    const resolved = resolveTypeScriptPath(resolvePath(sourceRoot, specifier.slice(2)));
    if (resolved) return { url: pathToFileURL(resolved).href, shortCircuit: true };
  }
  if (specifier.startsWith('.') && context.parentURL?.startsWith('file:')) {
    const unresolved = fileURLToPath(new URL(specifier, context.parentURL));
    const resolved = resolveTypeScriptPath(unresolved);
    if (resolved) return { url: pathToFileURL(resolved).href, shortCircuit: true };
  }
  return nextResolve(specifier, context);
}

export async function load(url, context, nextLoad) {
  if (url === 'semantic-test:request') {
    return { format: 'module', source: requestModuleSource, shortCircuit: true };
  }
  if (url === 'semantic-test:auth') {
    return { format: 'module', source: authModuleSource, shortCircuit: true };
  }
  if (url === 'semantic-test:blob-request') {
    return { format: 'module', source: blobRequestModuleSource, shortCircuit: true };
  }
  return nextLoad(url, context);
}
