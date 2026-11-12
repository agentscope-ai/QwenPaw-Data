import { App } from 'antd';

/** Prefer the ConfigProvider-scoped message holder so toasts are actually visible. */
export function useAppMessage() {
  return App.useApp();
}
