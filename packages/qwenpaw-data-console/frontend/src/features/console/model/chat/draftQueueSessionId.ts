interface CryptoRandomSource {
  randomUUID?: () => string;
  getRandomValues?: (array: Uint8Array) => Uint8Array;
}

let fallbackSequence = 0;

function createUuidFromRandomValues(source: CryptoRandomSource): string | null {
  if (typeof source.getRandomValues !== 'function') return null;

  const bytes = source.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;

  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0'));
  return [
    hex.slice(0, 4).join(''),
    hex.slice(4, 6).join(''),
    hex.slice(6, 8).join(''),
    hex.slice(8, 10).join(''),
    hex.slice(10, 16).join(''),
  ].join('-');
}

export function createDraftQueueSessionId(
  source: CryptoRandomSource | null | undefined = globalThis.crypto,
): string {
  if (typeof source?.randomUUID === 'function') {
    return `draft-queue-${source.randomUUID.call(source)}`;
  }

  if (source) {
    const uuid = createUuidFromRandomValues(source);
    if (uuid) return `draft-queue-${uuid}`;
  }

  fallbackSequence = (fallbackSequence + 1) % Number.MAX_SAFE_INTEGER;
  return `draft-queue-${Date.now().toString(36)}-${fallbackSequence.toString(36)}`;
}
