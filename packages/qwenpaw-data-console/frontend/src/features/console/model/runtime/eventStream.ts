import type { EventStream } from '../../api/types.js';

export function isMainStream(value: { stream?: EventStream }): boolean {
  return !value.stream || value.stream.type === 'main';
}

export function eventStreamKey(value: { stream?: EventStream }): string {
  return value.stream?.type === 'subagent' ? `subagent:${value.stream.parent_call_id}` : 'main';
}
