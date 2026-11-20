import { useEffect, type ReactNode } from 'react';
import { useLocation } from 'react-router-dom';
import type { EmbeddedModuleContext } from './contracts';

export interface LocalAction {
  type: string; // Whitelisted action name.
  value?: string | string[]; // Fill/select value.
  anchor_id?: string; // Registered anchor.
  visible?: boolean; // Drawer visibility.
  expanded?: boolean; // Registered expansion.
  tab_id?: string; // Registered tab.
}
export interface LocalElement {
  element_id: string; // Stable registry key.
  allowed_actions: string[]; // Explicit allowlist.
  value_type?: string; // Value kind.
  options?: { id: string; label: string }[]; // Real selectable options.
}
export interface UiBridge {
  assertClean: () => void; // Refuse navigation or replacement while a draft is dirty.
  register: (
    element: LocalElement,
    handler: (
      action: LocalAction,
      context: EmbeddedModuleContext,
    ) => Promise<Record<string, unknown>>,
  ) => () => void; // Mount lifetime.
  object: (
    kind: string,
    handler: (
      ref: Record<string, unknown>,
      context: EmbeddedModuleContext,
    ) => Promise<Record<string, unknown>>,
  ) => () => void; // Object handler.
  touch: () => void; // Invalidate previous inspection.
  ready: (path: string) => void; // A permission-guarded page committed.
  wait: (predicate: () => boolean, signal: AbortSignal) => Promise<void>; // Event-driven readiness.
  dirty: (key: string, value: boolean) => void; // Protect manual/automatic unsaved drafts.
}
let bridge: UiBridge | undefined;
export function configureUiBridge(value: UiBridge) {
  bridge = value;
}
export function getUiBridge() {
  return bridge;
}
export function PageReady({ children }: { children: ReactNode }) {
  const location = useLocation();
  useEffect(() => {
    bridge?.ready(location.pathname);
  }, [location.pathname]);
  return <>{children}</>;
}
