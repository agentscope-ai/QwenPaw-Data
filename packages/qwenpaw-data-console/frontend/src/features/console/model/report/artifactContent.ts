/** File absence is distinct from an inaccessible or nonexistent session. */
export function isMissingArtifact(error: {
  status: number;
  code: string | null;
  message: string;
}): boolean {
  return (
    error.status === 404 &&
    error.code === 'NOT_FOUND' &&
    error.message === 'workspace file not found'
  );
}

export function parseArtifactJson<T>(text: string | null, path: string): T | null {
  if (text === null) return null;
  try {
    return JSON.parse(text) as T;
  } catch (cause) {
    const error = new Error(
      `Invalid JSON in ${path}: ${cause instanceof Error ? cause.message : String(cause)}`,
    );
    Object.assign(error, { cause });
    throw error;
  }
}
