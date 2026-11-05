/* eslint-disable react-refresh/only-export-components -- Runtime tool renderers are exported as a registry consumed by AgentScopeRuntimeWebUI. */
import type { IAgentScopeRuntimeMessage } from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/types';
import type { IAgentScopeRuntimeWebUICardsOptions } from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/types';
import {
  CheckCircleFilled,
  DownOutlined,
  LoadingOutlined,
} from '@ant-design/icons';
import Markdown from '@agentscope-ai/chat/lib/Markdown';
import type { FC } from 'react';
import { useEffect, useState } from 'react';
import { serializePreviewPayload } from '../model/markdownContent';
import {
  DATA_CONSOLE_BIZ_EVENT_COMPONENT,
  DATA_CONSOLE_SEGMENT_COMPONENT,
} from '../model/runtimeProjection';
import styles from '../index.module.less';

interface RuntimeToolCardProps {
  data: IAgentScopeRuntimeMessage;
}

interface ToolContentData {
  name?: string;
  arguments?: unknown;
  output?: unknown;
}

function parseToolData(data: IAgentScopeRuntimeMessage) {
  const items = data.content
    .filter((item) => item.type === 'data')
    .map((item) => item.data as ToolContentData);
  const call = items.find((item) => item.arguments !== undefined) ?? items[0] ?? {};
  const output = items.find((item) => item.output !== undefined) ?? {};

  return {
    code: call.name ?? output.name ?? 'tool',
    arguments: call.arguments,
    output: output.output,
  };
}

function formatToolValue(value: unknown): string {
  const payload = serializePreviewPayload(value);
  if (typeof value === 'string') return payload;
  return `\`\`\`json\n${payload}\n\`\`\``;
}

export function RuntimeToolCard({
  data,
}: RuntimeToolCardProps) {
  const [open, setOpen] = useState(data.status === 'in_progress');
  const tool = parseToolData(data);
  const running = data.status === 'in_progress';

  useEffect(() => {
    setOpen(running);
  }, [running]);

  if (hiddenRuntimeToolNames.has(tool.code)) return null;

  return (
    <section
      className={`${styles.runtimeToolCard} ${running ? styles.runtimeToolCardRunning : styles.runtimeToolCardSuccess}`}
      data-runtime-process="tool"
    >
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <span
          className={`${styles.runtimeToolStatusIcon} ${running ? styles.runtimeToolStatusProcessing : styles.runtimeToolStatusFinished}`}
          role="img"
          aria-label={running ? 'Processing' : 'Finished'}
        >
          {running ? <LoadingOutlined spin /> : <CheckCircleFilled />}
        </span>
        <strong className={styles.runtimeToolName}>{tool.code}</strong>
        <DownOutlined
          className={`${styles.collapseChevron} ${open ? styles.collapseChevronOpen : ''}`}
        />
      </button>
      <div
        className={`${styles.runtimeToolDetails} ${open ? styles.runtimeToolDetailsOpen : ''}`}
        aria-hidden={!open}
      >
        <div className={styles.runtimeToolDetailsInner}>
          {open && tool.arguments !== undefined ? (
            <div>
              <span>输入</span>
              <div className={styles.runtimeToolMarkdown}>
                <Markdown content={formatToolValue(tool.arguments)} />
              </div>
            </div>
          ) : null}
          {open && tool.output !== undefined ? (
            <div>
              <span>输出</span>
              <div className={styles.runtimeToolMarkdown}>
                <Markdown content={formatToolValue(tool.output)} />
              </div>
            </div>
          ) : null}
        </div>
      </div>
    </section>
  );
}

const createToolRenderer = (): FC<{ data: IAgentScopeRuntimeMessage }> => function ToolRenderer({ data }) {
  return <RuntimeToolCard data={data} />;
};

const HiddenRuntimeTool = () => null;

const hiddenRuntimeToolNames = new Set([
  DATA_CONSOLE_BIZ_EVENT_COMPONENT,
  DATA_CONSOLE_SEGMENT_COMPONENT,
  'emit_semantic_refs',
  'update_status',
]);

const defaultToolRenderer = createToolRenderer();

export const runtimeToolRenderConfig = new Proxy(
  {},
  {
    get: (_target, toolName) => (
      typeof toolName === 'string' && hiddenRuntimeToolNames.has(toolName)
        ? HiddenRuntimeTool
        : defaultToolRenderer
    ),
  },
) as IAgentScopeRuntimeWebUICardsOptions;
