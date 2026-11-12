import { Tooltip } from 'antd';
import { useAppI18n } from '@/i18n';
import { formatUsageNumber } from '@/features/console/model/usage/usagePresentation';

export default function UsageValue({
  value,
  compact = true,
}: {
  value: number;
  compact?: boolean;
}) {
  const { language } = useAppI18n();
  const exact = formatUsageNumber(value, language, false);
  return (
    <Tooltip title={exact}>
      <span tabIndex={compact && value >= 1000 ? 0 : undefined} aria-label={exact}>
        {formatUsageNumber(value, language, compact)}
      </span>
    </Tooltip>
  );
}
