export const runtimeQueueSteerOwnerOnly = false;

export function isRuntimeChatRunning(chatStatus: string | null | undefined): boolean {
  return chatStatus === 'running';
}

export function shouldUseRuntimeInputQueue(): boolean {
  // Keep the SDK queue controller active before the first direct request. It
  // records that request's queue-session ownership, which prevents a later
  // enqueue during SSE from being mistaken for a peer-tab takeover/reconnect.
  return true;
}

export function shouldShowRuntimeStopButton(
  chatStatus: string | null | undefined,
  inputValue: string,
): boolean {
  return isRuntimeChatRunning(chatStatus) && inputValue.trim().length === 0;
}

export function isRuntimeQueueSteerVisible(chatStatus: string | null | undefined): boolean {
  return isRuntimeChatRunning(chatStatus);
}
