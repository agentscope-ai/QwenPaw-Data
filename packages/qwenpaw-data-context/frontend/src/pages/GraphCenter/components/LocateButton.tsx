import { Button } from '@/design';
import { useTranslation } from 'react-i18next';

/** Crosshair icon used by the Locate flow. `size` is sanitized because antd
 *  Button clones the icon element and may inject its own size ("middle"). */
export function CrosshairIcon({ size = 15 }: { size?: number | string }) {
  const s = typeof size === 'number' && Number.isFinite(size) ? size : 15;
  return (
    <svg
      className="gc-ico-crosshair"
      width={s}
      height={s}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="7" />
      <path d="M12 1.6v3.8M12 18.6v3.8M1.6 12h3.8M18.6 12h3.8" />
      <circle cx="12" cy="12" r="1.7" fill="currentColor" stroke="none" />
    </svg>
  );
}

interface LocateButtonProps {
  nodeId: string;
  onLocate: (nodeId: string) => void;
  small?: boolean;
}

/** "Locate" — jump to the global graph and center/focus this node. */
export default function LocateButton({ nodeId, onLocate, small }: LocateButtonProps) {
  const { t } = useTranslation();
  return (
    <Button
      size={small ? 'small' : 'middle'}
      icon={<CrosshairIcon />}
      title={t('graphCenter.locateTooltip')}
      onClick={() => onLocate(nodeId)}
    >
      Locate
    </Button>
  );
}
