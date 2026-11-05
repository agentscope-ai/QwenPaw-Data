import type {
  IAgentScopeRuntimeMessage,
  ITextContent,
} from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/types';
import {
  AgentScopeRuntimeContentType,
  AgentScopeRuntimeRunStatus,
} from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/types';
import {
  BulbOutlined,
  DownOutlined,
  LoadingOutlined,
} from '@ant-design/icons';
import { useEffect, useState } from 'react';
import styles from '../index.module.less';
import OverflowTooltip from './OverflowTooltip';

function thinkingText(message: IAgentScopeRuntimeMessage): string {
  return message.content
    .filter((item): item is ITextContent => (
      item.type === AgentScopeRuntimeContentType.TEXT
    ))
    .map((item) => item.text)
    .join('\n')
    .trim();
}

export default function RuntimeThinkingCard({
  data,
}: {
  data: IAgentScopeRuntimeMessage;
}) {
  const running = data.status === AgentScopeRuntimeRunStatus.InProgress;
  const [open, setOpen] = useState(running);
  const content = thinkingText(data);

  useEffect(() => {
    setOpen(running);
  }, [running]);

  if (!content) return null;

  return (
    <section
      className={`${styles.runtimeThinkingCard} ${running ? styles.runtimeThinkingCardRunning : ''}`}
      data-runtime-process="thinking"
    >
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <span className={styles.runtimeThinkingIcon} aria-hidden="true">
          {running ? <LoadingOutlined spin /> : <BulbOutlined />}
        </span>
        <span className={styles.runtimeThinkingTitle}>
          {running ? '正在思考' : '思考过程'}
        </span>
        <OverflowTooltip title={running ? '分析中' : '已完成'}>
          <span className={styles.runtimeThinkingStatus}>
            {running ? '分析中' : '已完成'}
          </span>
        </OverflowTooltip>
        <DownOutlined
          className={`${styles.collapseChevron} ${open ? styles.collapseChevronOpen : ''}`}
        />
      </button>
      <div
        className={`${styles.runtimeThinkingDetails} ${open ? styles.runtimeThinkingDetailsOpen : ''}`}
        aria-hidden={!open}
      >
        <div>{content}</div>
      </div>
    </section>
  );
}
