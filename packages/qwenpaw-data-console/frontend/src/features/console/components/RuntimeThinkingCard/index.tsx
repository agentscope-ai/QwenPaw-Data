import type {
  IAgentScopeRuntimeMessage,
  ITextContent,
} from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/types';
import {
  AgentScopeRuntimeContentType,
  AgentScopeRuntimeRunStatus,
} from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/types';
import { BulbOutlined, DownOutlined, LoadingOutlined } from '@ant-design/icons';
import { useEffect, useState } from 'react';
import sharedStyles from '../../index.module.less';
import { mergeStyleModules } from '../../styleModules';
import { useAppI18n } from '@/i18n';
import OverflowTooltip from '../OverflowTooltip';
import ownerStyles from './index.module.less';

const styles = mergeStyleModules(ownerStyles, sharedStyles);

function thinkingText(message: IAgentScopeRuntimeMessage): string {
  return message.content
    .filter((item): item is ITextContent => item.type === AgentScopeRuntimeContentType.TEXT)
    .map((item) => item.text)
    .join('\n')
    .trim();
}

export default function RuntimeThinkingCard({ data }: { data: IAgentScopeRuntimeMessage }) {
  const { t } = useAppI18n();
  const running = data.status === AgentScopeRuntimeRunStatus.InProgress;
  const [open, setOpen] = useState(running);
  const content = thinkingText(data);

  useEffect(() => {
    setOpen(running);
  }, [running]);

  if (!content) return null;

  return (
    <section
      className={`${styles.runtimeThinkingCard} ${open ? styles.runtimeThinkingCardOpen : ''} ${running ? styles.runtimeThinkingCardRunning : ''}`}
      data-runtime-process="thinking"
    >
      <button type="button" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
        <DownOutlined
          className={`${styles.collapseChevron} ${open ? styles.collapseChevronOpen : ''}`}
        />
        <span className={styles.runtimeThinkingIcon} aria-hidden="true">
          {running ? <LoadingOutlined spin /> : <BulbOutlined />}
        </span>
        <span className={styles.runtimeThinkingTitle}>
          {running ? t('runtime.thinking') : t('runtime.thoughtProcess')}
        </span>
        <OverflowTooltip title={running ? t('runtime.analyzing') : t('runtime.done')}>
          <span className={styles.runtimeThinkingStatus}>
            {running ? t('runtime.analyzing') : t('runtime.done')}
          </span>
        </OverflowTooltip>
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
