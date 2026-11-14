import type {
  IAgentScopeRuntimeMessage,
  IContent,
} from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/types';
import type { MessageContent } from '../../api/types';
import { selectRuntimeProjectionPayload } from '../runtime/runtimeProjection.js';

function escapeLabel(value: string): string {
  return value.replace(/([\\[\]])/g, '\\$1');
}

function markdownLink(label: string, url: string): string {
  return `[${escapeLabel(label)}](<${url}>)`;
}

function fencedContent(value: unknown, language = ''): string {
  const content = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  return `~~~${language}\n${content ?? String(value)}\n~~~`;
}

function runtimeContentToMarkdown(content: IContent): string {
  switch (content.type) {
    case 'text':
      return content.text;
    case 'refusal':
      return content.refusal;
    case 'image':
      return `![图片](<${content.image_url}>)`;
    case 'video':
      return markdownLink('视频', content.video_url);
    case 'audio': {
      const url = content.audio_url || content.data;
      return url ? markdownLink('音频', url) : '';
    }
    case 'file': {
      const name = content.file_name || content.fileName || content.file_id || '文件';
      return content.file_url
        ? markdownLink(name, content.file_url)
        : `**文件：** ${escapeLabel(name)}`;
    }
    case 'data':
      return fencedContent(content.data, 'json');
    default:
      return fencedContent(content, 'json');
  }
}

const runtimeMessageLabels: Record<string, string> = {
  reasoning: '思考过程',
  message: '回复',
  tool_call: 'Tool Use',
  function_call: 'Tool Use',
  plugin_call: 'Tool Use',
  component_call: 'Tool Use',
  mcp_call: 'Tool Use',
  tool_call_output: 'Tool Result',
  function_call_output: 'Tool Result',
  plugin_call_output: 'Tool Result',
  component_call_output: 'Tool Result',
  mcp_call_output: 'Tool Result',
  error: '错误',
};

function runtimeMessageToolName(message: IAgentScopeRuntimeMessage): string | null {
  const dataContent = message.content.find((content) => content.type === 'data');
  if (!dataContent || !dataContent.data || typeof dataContent.data !== 'object') return null;
  const name = 'name' in dataContent.data ? dataContent.data.name : null;
  return typeof name === 'string' && name.trim() ? name.trim() : null;
}

function runtimeMessageToMarkdown(message: IAgentScopeRuntimeMessage): string {
  const projection = selectRuntimeProjectionPayload(message);
  if (projection?.kind === 'segment') {
    const { segment } = projection;
    const artifacts = (segment.artifact ?? []).map(
      (artifact) => `- ${artifact.name}：${artifact.description}（${artifact.relative_path}）`,
    );
    return [
      `## Segment ${segment.coverage.start_seq}–${segment.coverage.end_seq} · ${segment.title}`,
      `**输入**\n\n${segment.input?.trim() || '无输入'}`,
      `**执行**\n\n${segment.behavior}`,
      `**结论**\n\n${segment.conclusion}`,
      artifacts.length ? `**产物**\n\n${artifacts.join('\n')}` : '',
    ]
      .filter(Boolean)
      .join('\n\n');
  }
  if (projection?.kind === 'biz_event') {
    const { event } = projection;
    return [
      `## 业务轨迹 · ${event.presentation?.caption || '业务步骤'}`,
      `BizEvent · seq ${event.seq}`,
      event.presentation?.body || '_暂无详细内容_',
    ].join('\n\n');
  }

  const label = runtimeMessageLabels[message.type] ?? message.type;
  const toolName = runtimeMessageToolName(message);
  const heading = `## ${label}${toolName ? ` · ${toolName}` : ''}`;
  const body = message.content
    .map(runtimeContentToMarkdown)
    .map((content) => content.trim())
    .filter(Boolean)
    .join('\n\n');
  const fallback = message.message ? `> ${message.message}` : '_无内容_';
  return `${heading}\n\n${body || fallback}`;
}

export function serializeRuntimeResponseToMarkdown(output: IAgentScopeRuntimeMessage[]): string {
  return output.map(runtimeMessageToMarkdown).join('\n\n').trim();
}

function traceContentToMarkdown(content: MessageContent): string {
  if (content.text) return content.text;
  if (content.refusal) return content.refusal;
  if (content.image_url) return `![图片](<${content.image_url}>)`;
  if (content.video_url) return markdownLink('视频', content.video_url);
  if (content.file_url) {
    return markdownLink(content.filename || '文件', content.file_url);
  }
  if (content.filename) return `**文件：** ${escapeLabel(content.filename)}`;
  if (content.data !== null && content.data !== undefined) {
    return content.format === 'markdown' && typeof content.data === 'string'
      ? content.data
      : fencedContent(content.data, typeof content.data === 'object' ? 'json' : 'text');
  }
  return content.type ? `_${content.type}_` : '';
}

export function serializeTraceContentToMarkdown(contents: MessageContent[]): string {
  return contents
    .map(traceContentToMarkdown)
    .map((content) => content.trim())
    .filter(Boolean)
    .join('\n\n')
    .trim();
}
