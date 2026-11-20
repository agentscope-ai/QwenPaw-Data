import { Tag } from '@/design';
import type { TFunction } from 'i18next';
import { PROFILING_STATUS_COLORS, statusText } from './status';
import type { ProfilingStatusValue } from './status';

export default function ProfilingStatusTag({
  t,
  status,
  label,
  size = 'small',
}: {
  t: TFunction;
  status?: ProfilingStatusValue | null;
  label?: string;
  size?: 'small' | 'middle';
}) {
  if (!status) return <span>-</span>;
  return (
    <Tag size={size} color={PROFILING_STATUS_COLORS[status] ?? 'default'}>
      {label ?? statusText(t, status)}
    </Tag>
  );
}
