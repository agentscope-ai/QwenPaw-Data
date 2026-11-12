import { LiquidGlass } from '@liquidglass/react';
import { useState } from 'react';
import type { PropsWithChildren } from 'react';
import { supportsConsoleLiquidGlass } from '../../model/runtime/liquidGlassSupport';
import styles from './index.module.less';

export default function ConsoleGlass({
  children,
  className,
}: PropsWithChildren<{ className?: string }>) {
  const [supported] = useState(supportsConsoleLiquidGlass);

  if (!supported) {
    return <div className={`${className ?? ''} ${styles.fallback}`}>{children}</div>;
  }

  return (
    <LiquidGlass
      borderRadius={0}
      blur={10}
      contrast={1.08}
      brightness={1.06}
      saturation={1.24}
      shadowIntensity={0}
      displacementScale={0.42}
      elasticity={0.38}
      zIndex={4}
      className={className}
    >
      {children}
    </LiquidGlass>
  );
}
