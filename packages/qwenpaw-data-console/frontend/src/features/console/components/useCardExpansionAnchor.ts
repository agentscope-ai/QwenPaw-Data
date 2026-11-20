import { useCallback, useEffect, useRef } from 'react';

// Nested cards share one scroll container. Only the latest toggle may anchor it.
const activeAnchors = new WeakMap<HTMLElement, () => void>();

/** Keep a manually toggled card's header still inside the reversed chat list. */
export function useCardExpansionAnchor<T extends HTMLElement = HTMLElement>() {
  const cardRef = useRef<T | null>(null);
  const cleanupRef = useRef<(() => void) | null>(null);
  useEffect(() => () => cleanupRef.current?.(), []);

  const preserveHeader = useCallback(() => {
    cleanupRef.current?.();
    const card = cardRef.current;
    const scroll = card?.closest<HTMLElement>('.data-runtime-bubble-list-scroll');
    if (!card || !scroll) return;
    activeAnchors.get(scroll)?.();
    const top = card.getBoundingClientRect().top;
    const previousAnchor = scroll.style.overflowAnchor;
    scroll.style.overflowAnchor = 'none';
    const observer = new ResizeObserver(() => {
      const delta = card.getBoundingClientRect().top - top;
      if (Math.abs(delta) > 0.5) scroll.scrollTop += delta;
    });
    const cleanup = () => {
      observer.disconnect();
      window.clearTimeout(timer);
      scroll.style.overflowAnchor = previousAnchor;
      scroll.removeEventListener('wheel', cleanup);
      scroll.removeEventListener('touchstart', cleanup);
      scroll.removeEventListener('pointerdown', cleanup);
      scroll.removeEventListener('keydown', cleanup);
      cleanupRef.current = null;
      if (activeAnchors.get(scroll) === cleanup) activeAnchors.delete(scroll);
    };
    const timer = window.setTimeout(cleanup, 600);
    cleanupRef.current = cleanup;
    activeAnchors.set(scroll, cleanup);
    observer.observe(card);
    scroll.addEventListener('wheel', cleanup, { passive: true });
    scroll.addEventListener('touchstart', cleanup, { passive: true });
    scroll.addEventListener('pointerdown', cleanup);
    scroll.addEventListener('keydown', cleanup);
  }, []);

  return { cardRef, preserveHeader };
}
