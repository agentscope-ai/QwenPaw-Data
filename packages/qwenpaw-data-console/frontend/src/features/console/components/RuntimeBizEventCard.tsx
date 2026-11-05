import Markdown from '@agentscope-ai/chat/lib/Markdown';
import {
  BulbOutlined,
  CloseCircleFilled,
  DownOutlined,
  MessageOutlined,
  ToolOutlined,
} from '@ant-design/icons';
import { useState } from 'react';
import type { BizEvent, Presentation } from '../api/types';
import styles from '../index.module.less';

interface RuntimeBizEventCardProps {
  data: BizEvent;
}

function eventIcon(presentation: Presentation | null, failed: boolean) {
  if (failed) return <CloseCircleFilled />;
  if (presentation?.card_type === 'thinking') return <BulbOutlined />;
  if (presentation?.card_type === 'tool') return <ToolOutlined />;
  return <MessageOutlined />;
}

export default function RuntimeBizEventCard({
  data,
}: RuntimeBizEventCardProps) {
  const [open, setOpen] = useState(false);
  const failed = data.status === 'error';
  const title = data.presentation?.caption || '业务步骤';
  const body = data.presentation?.body?.trim() || '_暂无详细内容_';

  return (
    <section
      className={`${styles.runtimeBizEventCard} ${open ? styles.runtimeBizEventCardOpen : ''} ${failed ? styles.runtimeBizEventCardError : ''}`}
      data-biz-event-id={data.event_id}
    >
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <span className={styles.runtimeBizEventIcon} aria-hidden="true">
          {eventIcon(data.presentation, failed)}
        </span>
        <span className={styles.runtimeBizEventHeading}>
          <strong>{title}</strong>
        </span>
        <DownOutlined
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
