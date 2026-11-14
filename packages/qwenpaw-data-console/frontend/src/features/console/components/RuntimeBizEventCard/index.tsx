import { useCardExpansionAnchor } from '../useCardExpansionAnchor';
/* eslint-disable react-refresh/only-export-components -- Runtime tool renderers are exported as a registry consumed by AgentScopeRuntimeWebUI. */
import type { IAgentScopeRuntimeMessage } from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/types';
import type { IAgentScopeRuntimeWebUICardsOptions } from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/types';
import Markdown from '@agentscope-ai/chat/lib/Markdown';
import {
  BulbOutlined,
  CloseCircleFilled,
  DownOutlined,
  MessageOutlined,
  ToolOutlined,
} from '@ant-design/icons';
import type { FC } from 'react';
import { useEffect, useState } from 'react';
import type { BizEvent, Presentation, ExecutionTiming } from '../../api/types';
import ExecutionDuration from '../ExecutionDuration';
import {
  DATA_CONSOLE_BIZ_EVENT_COMPONENT,
  DATA_CONSOLE_SEGMENT_COMPONENT,
} from '../../model/runtime/runtimeProjection';
import sharedStyles from '../../index.module.less';
import { mergeStyleModules } from '../../styleModules';
import { useAppI18n } from '@/i18n';
import ownerStyles from './index.module.less';

const styles = mergeStyleModules(ownerStyles, sharedStyles);

interface RuntimeBizEventCardProps {
  data: BizEvent;
  defaultOpen?: boolean;
}

interface RuntimeToolContentData {
  mcp_name?: string;
  call_id?: string;
  name?: string;
  arguments?: unknown;
  output?: unknown;
}

interface RuntimeToolBizEventCardProps {
  data: IAgentScopeRuntimeMessage;
}

const hiddenRuntimeToolNames = new Set([
  DATA_CONSOLE_BIZ_EVENT_COMPONENT,
  DATA_CONSOLE_SEGMENT_COMPONENT,
  'emit_semantic_refs',
  'update_status',
]);

function parseRuntimeToolData(data: IAgentScopeRuntimeMessage) {
  const items = data.content
    .filter((item) => item.type === 'data')
    .map((item) => item.data as RuntimeToolContentData);
  const call = items.find((item) => item.arguments !== undefined) ?? items[0] ?? {};
  const output = items.find((item) => item.output !== undefined) ?? {};

  return {
    mcpName: call.mcp_name,
    callId: call.call_id ?? output.call_id ?? data.id,
    code: call.name ?? output.name ?? 'tool',
    arguments: call.arguments,
    output: output.output,
  };
}

function formatRuntimeToolValue(value: unknown): string {
  if (typeof value === 'string') return value;
  try {
    return `\`\`\`json\n${JSON.stringify(value, null, 2)}\n\`\`\``;
  } catch {
    return String(value);
  }
}

function runtimeToolBody(
  tool: ReturnType<typeof parseRuntimeToolData>,
  inputLabel: string,
  outputLabel: string,
): string {
  const sections: string[] = [];
  if (tool.arguments !== undefined) {
    sections.push(`**${inputLabel}**\n\n${formatRuntimeToolValue(tool.arguments)}`);
  }
  if (tool.output !== undefined) {
    sections.push(`**${outputLabel}**\n\n${formatRuntimeToolValue(tool.output)}`);
  }
  return sections.join('\n\n');
}

export function runtimeToolMessageToBizEvent(
  data: IAgentScopeRuntimeMessage,
  inputLabel: string,
  outputLabel: string,
): BizEvent {
  const tool = parseRuntimeToolData(data);
  const terminal = data.status !== 'in_progress';

  return {
    event_id: data.id,
    timing: (data as IAgentScopeRuntimeMessage & { timing?: ExecutionTiming }).timing,
    seq: 0,
    channel: 'main',
    block_id: tool.callId,
    status: data.status === 'failed' ? 'error' : 'done',
    presentation: {
      card_type: 'tool',
      caption: tool.code,
      body: runtimeToolBody(tool, inputLabel, outputLabel),
    },
    started_at: 0,
    ended_at: terminal ? 0 : null,
  };
}

function eventIcon(presentation: Presentation | null, failed: boolean) {
  if (failed) return <CloseCircleFilled />;
  if (presentation?.card_type === 'thinking') return <BulbOutlined />;
  if (presentation?.card_type === 'tool') return <ToolOutlined />;
  return <MessageOutlined />;
}

export default function RuntimeBizEventCard({
  data,
  defaultOpen = false,
}: RuntimeBizEventCardProps) {
  const { t } = useAppI18n();
  const running = data.presentation?.card_type === 'tool' && data.ended_at === null;
  const [open, setOpen] = useState(defaultOpen || running);
  const { cardRef, preserveHeader } = useCardExpansionAnchor();
  const failed = data.status === 'error';
  const title = data.presentation?.caption || t('runtime.businessStep');
  const body = data.presentation?.body?.trim() || t('runtime.noDetails');

  useEffect(() => {
    setOpen(defaultOpen || running);
  }, [defaultOpen, running]);

  if (data.presentation?.card_type === 'text') {
    return (
      <section className={styles.runtimeBizReply} data-biz-event-id={data.event_id}>
        {data.timing ? (
          <div className={styles.replyTiming}>
            <ExecutionDuration timing={data.timing} />
          </div>
        ) : null}
        <Markdown content={body} />
      </section>
    );
  }

  return (
    <section
      ref={cardRef}
      className={`${styles.runtimeBizEventCard} ${open ? styles.runtimeBizEventCardOpen : ''} ${failed ? styles.runtimeBizEventCardError : ''}`}
      data-biz-event-id={data.event_id}
    >
      <button
        type="button"
        aria-expanded={open}
        onClick={() => {
          preserveHeader();
          setOpen((value) => !value);
        }}
      >
        <span className={styles.runtimeBizEventIcon} aria-hidden="true">
          {eventIcon(data.presentation, failed)}
        </span>
        <span className={styles.runtimeBizEventHeading}>
          <strong>{title}</strong>
        </span>
        <ExecutionDuration timing={data.timing} />
        <DownOutlined
          aria-hidden="true"
          className={`${styles.collapseChevron} ${open ? styles.collapseChevronOpen : ''}`}
        />
      </button>
      <div
        className={`${styles.runtimeBizEventDetails} ${open ? styles.runtimeBizEventDetailsOpen : ''}`}
        aria-hidden={!open}
      >
        <div className={styles.runtimeBizEventDetailsInner}>
          <Markdown content={body} />
        </div>
      </div>
    </section>
  );
}

export function RuntimeToolBizEventCard({ data }: RuntimeToolBizEventCardProps) {
  const { t } = useAppI18n();
  const tool = parseRuntimeToolData(data);
  if (hiddenRuntimeToolNames.has(tool.code)) return null;

  const event = runtimeToolMessageToBizEvent(data, t('runtime.input'), t('runtime.output'));
  if (tool.mcpName) {
    event.presentation = {
      card_type: 'tool',
      caption: t('mcp.connectionFailed', { name: tool.mcpName }),
      body: [
        t('mcp.connectionFailedHelp'),
        runtimeToolBody(tool, t('runtime.input'), t('runtime.output')),
      ]
        .filter(Boolean)
        .join('\n\n'),
    };
  }
  return <RuntimeBizEventCard data={event} />;
}

const createToolRenderer = (): FC<{ data: IAgentScopeRuntimeMessage }> =>
  function ToolRenderer({ data }) {
    return <RuntimeToolBizEventCard data={data} />;
  };

const HiddenRuntimeTool = () => null;
const defaultToolRenderer = createToolRenderer();

export const runtimeToolRenderConfig = new Proxy(
  {},
  {
    get: (_target, toolName) =>
      typeof toolName === 'string' && hiddenRuntimeToolNames.has(toolName)
        ? HiddenRuntimeTool
        : defaultToolRenderer,
  },
) as IAgentScopeRuntimeWebUICardsOptions;
