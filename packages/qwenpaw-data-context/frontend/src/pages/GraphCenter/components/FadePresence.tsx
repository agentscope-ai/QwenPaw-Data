import type { ReactNode } from 'react';
import { usePresence } from './iosMotion';
import motion from './iosMotion.module.less';

type FadeKind = 'float' | 'page' | 'veil';

export default function FadePresence({
  open = true,
  kind = 'float',
  className,
  children,
}: {
  open?: boolean;
  kind?: FadeKind;
  className?: string;
  children: ReactNode;
}) {
  const { mounted, entered } = usePresence(open);
  if (!mounted) return null;
  return (
    <div
      className={[motion.presence, motion[kind], entered ? motion.open : '', className || ''].join(
        ' ',
      )}
    >
      {children}
    </div>
  );
}
