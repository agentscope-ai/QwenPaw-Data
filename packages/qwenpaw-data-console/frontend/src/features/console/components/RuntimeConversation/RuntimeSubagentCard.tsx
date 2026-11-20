import Markdown from '@agentscope-ai/chat/lib/Markdown/Markdown';
import { BranchesOutlined, DownOutlined } from '@ant-design/icons';
import { useState } from 'react';
import { useAppI18n } from '@/i18n';
import type { TraceMessage } from '../../api/types';
import { serializeTraceContentToMarkdown } from '../../model/artifacts/markdownContent';
import type { SubagentActivity } from '../../model/runtime/subagentActivity';
import sharedStyles from '../../index.module.less';
import { useCardExpansionAnchor } from '../useCardExpansionAnchor';
import styles from './RuntimeSubagentCard.module.less';

function toolData(message: TraceMessage) {
  const data = message.content.find((part) => part.type === 'data')?.data;
  return data && typeof data === 'object' ? (data as Record<string, unknown>) : null;
}

function latestPreview(messages: TraceMessage[]): string {
  for (let index = messages.length - 1; index >= 0; index--) {
    const message = messages[index];
    const data = toolData(message);
    const text = message.content.map((part) => part.text ?? part.refusal ?? '').join('');
    const preview =
      text.trim() ||
      (typeof data?.output === 'string' ? data.output.trim() : '') ||
      (typeof data?.name === 'string' ? data.name : '');
    if (preview) return preview.length > 220 ? `…${preview.slice(-220).trimStart()}` : preview;
  }
  return '';
}

export default function RuntimeSubagentCard({ activity }: { activity: SubagentActivity }) {
  const { t } = useAppI18n();
  const [view, setView] = useState<'initial' | 'expanded' | 'collapsed'>('initial');
  const expanded = view === 'expanded';
  const { cardRef, preserveHeader } = useCardExpansionAnchor();
  const preview = activity.error || latestPreview(activity.messages);
  return (
    <section ref={cardRef} className={styles.card} data-expanded={expanded}>
      <button
        type="button"
        className={styles.summary}
        aria-expanded={expanded}
        onClick={() => {
          preserveHeader();
          setView(expanded ? 'collapsed' : 'expanded');
        }}
      >
        <span className={styles.titleRow}>
          <BranchesOutlined className={styles.icon} aria-hidden="true" />
          <span className={styles.title}>{t('runtime.subagent')}</span>
          <span className={styles.status} data-status={activity.status}>
            {t(`runtime.subagentStatus.${activity.status}`)}
          </span>
          <DownOutlined
            className={`${styles.chevron} ${sharedStyles.collapseChevron} ${expanded ? sharedStyles.collapseChevronOpen : ''}`}
            aria-hidden="true"
          />
        </span>
        <span className={styles.previewMotion} aria-hidden={expanded}>
          <span className={styles.clip}>
            <span className={styles.preview}>
              {preview ||
                t(
                  activity.status === 'running'
                    ? 'runtime.subagentWaiting'
                    : 'runtime.subagentEmpty',
                )}
            </span>
          </span>
        </span>
      </button>
      <div
        className={styles.bodyMotion}
        aria-hidden={!expanded}
        ref={(element) => {
          if (element) element.inert = !expanded;
        }}
      >
        <div className={styles.clip}>
          {view !== 'initial' ? (
            <div className={styles.body}>
              {activity.messages.map((message) => {
                const body = serializeTraceContentToMarkdown(message.content);
                if (!body) return null;
                const name = toolData(message)?.name;
                const label =
                  message.type === 'reasoning'
                    ? t('runtime.subagentReasoning')
                    : message.type === 'plugin_call'
                      ? t('runtime.subagentTool')
                      : message.type === 'plugin_call_output'
                        ? t('runtime.output')
                        : t('runtime.subagentMessage');
                return (
                  <div key={message.id} className={styles.entry}>
                    <div className={styles.entryLabel}>
                      {label}
                      {typeof name === 'string' ? ` · ${name}` : ''}
                    </div>
                    <Markdown content={body} baseFontSize={13} baseLineHeight={1.55} />
                  </div>
                );
              })}
              {activity.error ? <div className={styles.entry}>{activity.error}</div> : null}
              {!activity.messages.some((message) => message.content.length) && !activity.error ? (
                <div className={styles.entryLabel}>
                  {t(
                    activity.status === 'running'
                      ? 'runtime.subagentWaiting'
                      : 'runtime.subagentEmpty',
                  )}
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
      </div>
    </section>
  );
}
