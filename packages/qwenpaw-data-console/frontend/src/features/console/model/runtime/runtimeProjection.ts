import type {
  AgentScopeRuntimeContentType,
  AgentScopeRuntimeMessageType,
  AgentScopeRuntimeRunStatus,
  IDataContent,
  IAgentScopeRuntimeMessage,
} from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/types';
import type { BizEvent, Segment } from '../../api/types';

export const DATA_CONSOLE_SEGMENT_COMPONENT = 'dataconsole_segment';
export const DATA_CONSOLE_BIZ_EVENT_COMPONENT = 'dataconsole_biz_event';

const dataContentType = 'data' as AgentScopeRuntimeContentType.DATA;
const componentCallType = 'component_call' as AgentScopeRuntimeMessageType.COMPONENT_CALL;
const completedStatus = 'completed' as AgentScopeRuntimeRunStatus.Completed;
const failedStatus = 'failed' as AgentScopeRuntimeRunStatus.Failed;

export type RuntimeProjectionPayload =
  | { kind: 'segment'; segment: Segment; events?: BizEvent[] }
  | { kind: 'biz_event'; event: BizEvent };

export function formatSegmentDuration(
  startedAt: number | null,
  endedAt: number | null,
): string | null {
  if (startedAt === null || endedAt === null) return null;
  const durationSeconds = Math.max(0, Math.round(endedAt - startedAt));
  const minutes = Math.floor(durationSeconds / 60);
  const seconds = durationSeconds % 60;
  return `用时 ${minutes}分${seconds}秒`;
}

function elapsedSeconds(start: number | null, end: number | null): number | null {
  if (start === null || end === null || !Number.isFinite(start) || !Number.isFinite(end))
    return null;
  return Math.max(0, end - start);
}

/** Segment title uses only the segment's own timestamps. */
export function segmentDurationSeconds(segment: Segment): number | null {
  return elapsedSeconds(segment.started_at, segment.ended_at);
}

/** Worked for uses the first/last event in sequence order, not timestamp extrema. */
export function eventDurationSeconds(events: readonly BizEvent[]): number | null {
  if (!events.length) return null;
  const ordered = [...events].sort((a, b) => a.seq - b.seq);
  return elapsedSeconds(ordered[0].started_at, ordered[ordered.length - 1].ended_at);
}

interface RuntimeProjectionData {
  call_id: string;
  name: string;
  arguments: RuntimeProjectionPayload;
}

function createRuntimeProjectionMessage(
  id: string,
  name: string,
  payload: RuntimeProjectionPayload,
  status: AgentScopeRuntimeRunStatus,
): IAgentScopeRuntimeMessage {
  return {
    id,
    object: 'message',
    role: 'assistant',
    type: componentCallType,
    content: [
      {
        type: dataContentType,
        data: {
          call_id: id,
          name,
          arguments: payload,
        } satisfies RuntimeProjectionData,
        status,
      },
    ],
    status,
  };
}

export function createSegmentRuntimeMessage(
  segment: Segment,
  events?: BizEvent[],
): IAgentScopeRuntimeMessage {
  return createRuntimeProjectionMessage(
    segment.segment_id,
    DATA_CONSOLE_SEGMENT_COMPONENT,
    { kind: 'segment', segment, ...(events ? { events } : {}) },
    completedStatus,
  );
}

export function createBizEventRuntimeMessage(event: BizEvent): IAgentScopeRuntimeMessage {
  return createRuntimeProjectionMessage(
    event.event_id,
    DATA_CONSOLE_BIZ_EVENT_COMPONENT,
    { kind: 'biz_event', event },
    event.status === 'error' ? failedStatus : completedStatus,
  );
}

function isProjectionContent(
  content: IAgentScopeRuntimeMessage['content'][number],
): content is IDataContent<RuntimeProjectionData> {
  if (content.type !== dataContentType) return false;
  const data = content.data;
  if (!data || typeof data !== 'object') return false;
  if (!('name' in data) || !('arguments' in data)) return false;
  return (
    data.name === DATA_CONSOLE_SEGMENT_COMPONENT || data.name === DATA_CONSOLE_BIZ_EVENT_COMPONENT
  );
}

export function selectRuntimeProjectionPayload(
  message: IAgentScopeRuntimeMessage,
): RuntimeProjectionPayload | null {
  if (message.type !== componentCallType) return null;
  const content = message.content.find(isProjectionContent);
  if (!content) return null;
  const payload = content.data.arguments;

  if (content.data.name === DATA_CONSOLE_SEGMENT_COMPONENT) {
    return payload?.kind === 'segment' && payload.segment ? payload : null;
  }
  return payload?.kind === 'biz_event' && payload.event ? payload : null;
}
