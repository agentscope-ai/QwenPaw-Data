import type { McpConnectEvent, TraceMessage } from '../../api/types';

/** Connection probes precede agent messages; keep their SSE order after the user request. */
export function createMcpFailureMessage(event: McpConnectEvent): TraceMessage | null {
  if (
    event.status !== 'failed' ||
    typeof event.mcp_name !== 'string' ||
    !event.mcp_name.trim() ||
    !Number.isSafeInteger(event.sequence_number) ||
    event.sequence_number < 0
  )
    return null;
  const id = `mcp-connect:${event.chat_id}:${event.sequence_number}`;
  return {
    id,
    object: 'message',
    chat_id: event.chat_id,
    sequence: 1 - 1 / (event.sequence_number + 2),
    type: 'plugin_call_output',
    role: 'tool',
    status: 'failed',
    metadata: { mcp_connection: true },
    content: [
      {
        object: 'content',
        type: 'data',
        delta: false,
        index: 0,
        status: 'failed',
        msg_id: id,
        text: null,
        image_url: null,
        format: null,
        video_url: null,
        filename: null,
        file_url: null,
        refusal: null,
        data: {
          call_id: id,
          name: `MCP ${event.mcp_name}`,
          mcp_name: event.mcp_name,
          output: typeof event.error === 'string' ? event.error : '',
        },
      },
    ],
  };
}
