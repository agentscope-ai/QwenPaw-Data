import type { ConsoleStreamEvent, ExecutionTiming, Segment, TimingEvent } from '../../api/types.js';

const timingObjects = new Set([
  'model.call',
  'model.call.end',
  'tool.result.start',
  'tool.result.end',
  'message',
]);

export function toTimingEvent(event: ConsoleStreamEvent): TimingEvent | null {
  if (!timingObjects.has(event.object)) return null;
  const base: TimingEvent = {
    object: event.object,
    session_id: event.session_id,
    chat_id: event.chat_id,
    sequence_number: event.sequence_number,
    created_at: event.created_at,
  };
  if (event.object === 'message') {
    return {
      ...base,
      id: event.id,
      type: event.type,
      role: event.role,
      source_id: event.source_id,
    };
  }
  if (event.object === 'tool.result.start' || event.object === 'tool.result.end') {
    return { ...base, call_id: event.call_id };
  }
  return base;
}

export function mergeTimingEvents(
  current: readonly TimingEvent[],
  incoming: readonly TimingEvent[],
  chat: { id: string; session_id: string },
): TimingEvent[] {
  const events = new Map<number, TimingEvent>();
  // Live records take precedence over a delayed HTTP response.
  for (const event of [...incoming, ...current]) {
    if (
      event.chat_id === chat.id &&
      event.session_id === chat.session_id &&
      Number.isFinite(event.sequence_number) &&
      timingObjects.has(event.object)
    ) {
      events.set(event.sequence_number, event);
    }
  }
  return [...events.values()].sort((a, b) => a.sequence_number - b.sequence_number);
}

function timestamp(event: TimingEvent): number | null {
  const value = event.created_at ? Date.parse(event.created_at) / 1000 : NaN;
  return Number.isFinite(value) ? value : null;
}

interface ModelSpan {
  start: number | null;
  end: number | null;
  blocks: Set<string>;
}

export function indexExecutionTimings(events: readonly TimingEvent[]) {
  const models: ModelSpan[] = [];
  const tools = new Map<string, ExecutionTiming>();
  const toolStarts = new Map<string, number>();
  let model: ModelSpan | undefined;
  for (const event of events) {
    const at = timestamp(event);
    if (event.object === 'model.call') {
      model = { start: at, end: null, blocks: new Set() };
      models.push(model);
    } else if (event.object === 'model.call.end') {
      if (model && model.end === null) model.end = at;
    } else if (event.object === 'message' && event.source_id && event.role === 'assistant') {
      // The completed message survives persistence cleanup. Some block completions
      // follow model.call.end; keep the association until the next model.call.
      model?.blocks.add(event.source_id);
    } else if (event.object === 'tool.result.start' && event.call_id) {
      if (at !== null) toolStarts.set(event.call_id, at);
    } else if (event.object === 'tool.result.end' && event.call_id) {
      const start = toolStarts.get(event.call_id);
      if (start !== undefined && at !== null && at >= start) {
        tools.set(event.call_id, { kind: 'tool', startedAt: start, endedAt: at });
      }
      toolStarts.delete(event.call_id);
    }
  }
  const modelByBlock = new Map<string, ExecutionTiming>();
  for (const span of models) {
    if (span.start === null || span.end === null || span.end < span.start) continue;
    const timing: ExecutionTiming = { kind: 'model', startedAt: span.start, endedAt: span.end };
    for (const id of span.blocks) modelByBlock.set(id, timing);
  }
  return { models: modelByBlock, tools };
}

export function durationSeconds(timing: ExecutionTiming): number {
  return Math.max(0, timing.endedAt - timing.startedAt);
}

export function formatExecutionSeconds(seconds: number): string {
  if (seconds > 0 && seconds < 0.1) return '<0.1';
  return String(Math.round(seconds * 10) / 10);
}

export function otherDurationSeconds(
  workedFor: number | null,
  totals: Segment['timingTotals'],
): number | null {
  if (workedFor === null || !totals) return null;
  return Math.max(0, workedFor - totals.modelSeconds - totals.toolSeconds);
}

export function segmentDurationParts(seconds: number) {
  const rounded = Math.round(seconds * 10) / 10;
  return {
    minutes: Math.floor(rounded / 60),
    seconds: seconds > 0 && seconds < 0.1 ? '<0.1' : formatExecutionSeconds(rounded % 60),
  };
}
