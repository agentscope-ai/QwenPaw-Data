/** SSE client for POST /api/v3/cm/explore — the autonomous graph exploration
 *  agent (free tool-calling loop, see backend api/journey_agent.py).
 *
 *  EventSource can't do POST + auth headers, so this is a fetch +
 *  ReadableStream SSE parser. Returns an abort function. */
import { getApiUrl } from '../config';
import { buildAuthHeaders } from '../authHeaders';

export interface ExploreGraphPayloadNode {
  key: string;
  label: string;
  name: string;
  props?: Record<string, unknown>;
}

export interface ExploreGraphPayloadEdge {
  from: string;
  rel: string;
  to: string;
}

export interface ExploreGraphPayload {
  nodes: ExploreGraphPayloadNode[];
  edges: ExploreGraphPayloadEdge[];
}

export interface ExploreStartEvent {
  query: string;
  model: string;
  max_rounds: number;
}

export interface ExploreThinkingEvent {
  round: number;
  text: string;
}

export interface ExploreToolCallEvent {
  round: number;
  id: string;
  name: string;
  args: Record<string, unknown>;
}

export interface ExploreToolResultEvent {
  round: number;
  id: string;
  name: string;
  ok: boolean;
  /** Raw tool result JSON (string, possibly truncated server-side). */
  result: string;
  graph?: ExploreGraphPayload;
}

export interface ExploreFinalEvent {
  text: string;
  rounds: number;
  tool_calls: number;
  elapsed_ms: number;
}

export interface ExploreHandlers {
  onStart?: (e: ExploreStartEvent) => void;
  onThinking?: (e: ExploreThinkingEvent) => void;
  onToolCall?: (e: ExploreToolCallEvent) => void;
  onToolResult?: (e: ExploreToolResultEvent) => void;
  onBudget?: (e: { reason: string }) => void;
  onFinal?: (e: ExploreFinalEvent) => void;
  onError?: (message: string) => void;
  /** Transport-level failure (HTTP status / network) — before any SSE frame. */
  onConnectError?: (status: number, message: string) => void;
  onDone?: () => void;
}

/** Start an explore session. Returns a cancel function. */
export function startExplore(
  query: string,
  handlers: ExploreHandlers,
  opts?: { maxRounds?: number; signal?: AbortSignal; lang?: string },
): () => void {
  const controller = new AbortController();
  const outerSignal = opts?.signal;
  if (outerSignal) {
    if (outerSignal.aborted) controller.abort();
    else outerSignal.addEventListener('abort', () => controller.abort(), { once: true });
  }

  const url = getApiUrl('/v3/cm/explore');
  const dispatch = (event: string, data: unknown) => {
    try {
      switch (event) {
        case 'start':
          handlers.onStart?.(data as ExploreStartEvent);
          break;
        case 'thinking':
          handlers.onThinking?.(data as ExploreThinkingEvent);
          break;
        case 'tool_call':
          handlers.onToolCall?.(data as ExploreToolCallEvent);
          break;
        case 'tool_result':
          handlers.onToolResult?.(data as ExploreToolResultEvent);
          break;
        case 'budget':
          handlers.onBudget?.(data as { reason: string });
          break;
        case 'final':
          handlers.onFinal?.(data as ExploreFinalEvent);
          break;
        case 'error':
          handlers.onError?.((data as { message?: string }).message || 'unknown error');
          break;
        case 'done':
          handlers.onDone?.();
          break;
        default:
          break;
      }
    } catch {
      /* handler errors must not kill the stream loop */
    }
  };

  (async () => {
    let response: Response;
    try {
      response = await fetch(url, {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
          ...buildAuthHeaders(),
        },
        body: JSON.stringify({
          query,
          max_rounds: opts?.maxRounds ?? 8,
          lang: opts?.lang,
        }),
        signal: controller.signal,
      });
    } catch (err) {
      if (!controller.signal.aborted) {
        handlers.onConnectError?.(0, err instanceof Error ? err.message : String(err));
      }
      return;
    }
    if (!response.ok || !response.body) {
      let detail = '';
      try {
        const body = await response.json();
        detail =
          typeof body?.detail === 'string' ? body.detail : JSON.stringify(body?.detail || body);
      } catch {
        /* ignore */
      }
      handlers.onConnectError?.(response.status, detail || `HTTP ${response.status}`);
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buf = '';
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        // SSE frames are separated by a blank line
        let sep: number;
        while ((sep = buf.indexOf('\n\n')) >= 0) {
          const frame = buf.slice(0, sep);
          buf = buf.slice(sep + 2);
          let event = 'message';
          const dataLines: string[] = [];
          for (const line of frame.split('\n')) {
            if (line.startsWith('event:')) event = line.slice(6).trim();
            else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart());
          }
          if (!dataLines.length) continue;
          let parsed: unknown;
          try {
            parsed = JSON.parse(dataLines.join('\n'));
          } catch {
            continue;
          }
          dispatch(event, parsed);
        }
      }
    } catch (err) {
      if (!controller.signal.aborted) {
        handlers.onConnectError?.(0, err instanceof Error ? err.message : String(err));
      }
    }
  })();

  return () => controller.abort();
}
