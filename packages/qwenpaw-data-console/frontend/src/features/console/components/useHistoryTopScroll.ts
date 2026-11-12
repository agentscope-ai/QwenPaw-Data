import { useEffect, useRef, type RefObject } from 'react';

/** Both lists share the controller's request lock and history cursor. */
export function useHistoryTopScroll(
  rootRef: RefObject<HTMLElement | null>,
  enabled: boolean,
  load: (() => Promise<void>) | undefined,
  selector?: string,
) {
  const state = useRef({ enabled, load });
  state.current = { enabled, load };
  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const positions = new WeakMap<HTMLElement, number>();
    const position = (element: HTMLElement) =>
      getComputedStyle(element).flexDirection === 'column-reverse'
        ? element.scrollHeight - element.clientHeight + element.scrollTop
        : element.scrollTop;
    const target = (event: Event) => {
      if (!(event.target instanceof Element)) return null;
      return selector ? event.target.closest<HTMLElement>(selector) : root;
    };
    const maybeLoad = (element: HTMLElement) => {
      if (state.current.enabled && root.dataset.chatJump !== 'true' && position(element) <= 40)
        void state.current.load?.();
    };
    const onScroll = (event: Event) => {
      const element = target(event);
      if (!element) return;
      const current = position(element);
      const previous = positions.get(element);
      positions.set(element, current);
      if (previous !== undefined && current < previous) maybeLoad(element);
    };
    const onWheel = (event: WheelEvent) => {
      const element = target(event);
      if (!element || event.deltaY >= 0) return;
      positions.set(element, position(element));
      maybeLoad(element);
    };
    root.addEventListener('scroll', onScroll, true);
    root.addEventListener('wheel', onWheel, { passive: true });
    return () => {
      root.removeEventListener('scroll', onScroll, true);
      root.removeEventListener('wheel', onWheel);
    };
  }, [rootRef, selector]);
}
