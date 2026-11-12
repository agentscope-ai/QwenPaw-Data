/** Shared iOS / macOS motion tokens used by Graph Center floats and refreshes.
 *  Curve matches the Cypher console overlay: Apple's UIView ease-out. */
import { useEffect, useState } from 'react';

export const IOS_EASE = 'cubic-bezier(0.32, 0.72, 0, 1)';
export const PRESENCE_EXIT_MS = 420;

export const iosMotionTheme = {
  token: {
    motionEaseOutCirc: IOS_EASE,
    motionEaseInOutCirc: IOS_EASE,
    motionEaseOut: IOS_EASE,
    motionEaseInOut: IOS_EASE,
    motionEaseInQuint: IOS_EASE,
    motionEaseOutQuint: IOS_EASE,
    motionDurationFast: '0.32s',
    motionDurationMid: '0.42s',
    motionDurationSlow: '0.42s',
  },
};

/** Keep the node mounted through the exit curve, then play enter on the next frame. */
export function usePresence(open: boolean, exitMs = PRESENCE_EXIT_MS) {
  const [mounted, setMounted] = useState(open);
  const [entered, setEntered] = useState(false);

  useEffect(() => {
    if (open) {
      setMounted(true);
      return;
    }
    if (!mounted) return;
    const id = window.setTimeout(() => setMounted(false), exitMs);
    return () => window.clearTimeout(id);
  }, [open, mounted, exitMs]);

  useEffect(() => {
    if (!mounted || !open) {
      setEntered(false);
      return;
    }
    let inner = 0;
    const outer = window.requestAnimationFrame(() => {
      inner = window.requestAnimationFrame(() => setEntered(true));
    });
    return () => {
      window.cancelAnimationFrame(outer);
      window.cancelAnimationFrame(inner);
    };
  }, [mounted, open]);

  return { mounted, entered };
}
