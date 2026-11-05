import { Tooltip } from '@agentscope-ai/design';
import {
  cloneElement,
  useCallback,
  useLayoutEffect,
  useRef,
  useState,
} from 'react';
import type {
  MouseEventHandler,
  MutableRefObject,
  ReactElement,
  ReactNode,
  Ref,
} from 'react';

interface MeasurableChildProps {
  'data-overflow-tooltip'?: 'disabled' | 'enabled';
  onMouseEnter?: MouseEventHandler<HTMLElement>;
  ref?: Ref<HTMLElement>;
}

interface OverflowTooltipProps {
  children: ReactElement<MeasurableChildProps>;
  title: ReactNode;
}

function assignRef(ref: Ref<HTMLElement> | undefined, node: HTMLElement | null) {
  if (typeof ref === 'function') {
    ref(node);
  } else if (ref) {
    (ref as MutableRefObject<HTMLElement | null>).current = node;
  }
}

function hasOverflow(element: HTMLElement): boolean {
  return element.scrollWidth > element.clientWidth + 1
    || element.scrollHeight > element.clientHeight + 1;
}

export default function OverflowTooltip({
  children,
  title,
}: OverflowTooltipProps) {
  const triggerRef = useRef<HTMLElement | null>(null);
  const [overflowing, setOverflowing] = useState(false);

  const measure = useCallback(() => {
    const element = triggerRef.current;
    if (!element) return;
    const next = hasOverflow(element);
    setOverflowing((current) => (current === next ? current : next));
  }, []);

  const setTriggerRef = useCallback((node: HTMLElement | null) => {
    triggerRef.current = node;
    assignRef(children.props.ref, node);
  }, [children.props.ref]);

  const handleMouseEnter: MouseEventHandler<HTMLElement> = useCallback((event) => {
    measure();
    children.props.onMouseEnter?.(event);
  }, [children.props, measure]);

  useLayoutEffect(() => {
    measure();
    const element = triggerRef.current;
    if (!element || typeof ResizeObserver === 'undefined') return undefined;

    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [measure, title]);

  return (
    <Tooltip title={overflowing ? title : null}>
      {cloneElement(children, {
        'data-overflow-tooltip': overflowing ? 'enabled' : 'disabled',
        ref: setTriggerRef,
        onMouseEnter: handleMouseEnter,
      })}
    </Tooltip>
  );
}
