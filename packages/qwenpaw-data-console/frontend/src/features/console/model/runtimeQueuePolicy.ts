export const runtimeQueueSteerOwnerOnly = false;

export function isRuntimeChatRunning(
  chatStatus: string | null | undefined,
): boolean {
  return chatStatus === 'running';
}

export function shouldUseRuntimeInputQueue(
  chatStatus: string | null | undefined,
  hasQueuedInputs: boolean,
): boolean {
  return isRuntimeChatRunning(chatStatus) || hasQueuedInputs;
}

export function shouldShowRuntimeStopButton(
  chatStatus: string | null | undefined,
  inputValue: string,
): boolean {
  return isRuntimeChatRunning(chatStatus) && inputValue.trim().length === 0;
}

export function isRuntimeQueueSteerVisible(
  chatStatus: string | null | undefined,
): boolean {
  return isRuntimeChatRunning(chatStatus);
}
