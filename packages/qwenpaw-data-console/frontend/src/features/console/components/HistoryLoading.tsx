import { LoadingOutlined } from '@ant-design/icons';
import { useAppI18n } from '@/i18n';

export default function HistoryLoading() {
  const { t } = useAppI18n();
  return (
    <div
      role="status"
      aria-label={t('common.loading')}
      style={{
        display: 'flex',
        justifyContent: 'center',
        padding: '8px 12px',
        flexShrink: 0,
        color: 'var(--console-primary, #1677ff)',
      }}
    >
      <LoadingOutlined spin />
    </div>
  );
}
