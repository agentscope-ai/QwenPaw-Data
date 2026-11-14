import { CopyOutlined } from '@ant-design/icons';
import SparkIdLine from '@agentscope-ai/icons/src/js/SparkIdLine';
import { Button, Popover, Tooltip } from '@agentscope-ai/design';
import { message } from 'antd';
import { useAppI18n } from '@/i18n';
import styles from './index.module.less';

export default function SourceSession({ sessionId, title }: { sessionId: string; title?: string }) {
  const { t } = useAppI18n();
  if (!sessionId.trim()) return null;

  const copyId = async () => {
    try {
      await navigator.clipboard.writeText(sessionId);
      message.success(t('settlementCenter.sessionIdCopied'));
    } catch {
      message.error(t('runtime.copyFailed'));
    }
  };
  const label = t('settlementCenter.fromSession', {
    title: title?.trim() || t('settlementCenter.sessionTitleUnavailable'),
  });

  return (
    <div className={styles.sourceSession}>
      <Tooltip title={label}>
        <span className={styles.sourceSessionTitle}>{label}</span>
      </Tooltip>
      <Popover
        trigger={['hover', 'click']}
        placement="topLeft"
        content={
          <div className={styles.sessionIdPopover}>
            <span>SessionId：{sessionId}</span>
            <Button
              type="text"
              size="small"
              icon={<CopyOutlined />}
              aria-label={t('settlementCenter.copySessionId')}
              onClick={() => void copyId()}
            />
          </div>
        }
      >
        <Button
          type="text"
          size="small"
          className={styles.sessionIdButton}
          icon={<SparkIdLine />}
          aria-label={t('settlementCenter.sessionId')}
        />
      </Popover>
    </div>
  );
}
