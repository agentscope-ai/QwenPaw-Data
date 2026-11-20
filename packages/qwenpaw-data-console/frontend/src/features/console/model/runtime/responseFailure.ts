import type { ConsoleChat } from '../../api/types.js';

type ResponseState = {
  status?: string;
  error?: { code?: string; message?: string } | null;
};

export function selectResponseFailure(chat: ConsoleChat | undefined, response: ResponseState) {
  if ((chat?.status ?? response.status) !== 'failed') return null;
  const error = chat?.error ?? response.error;
  const code = error?.code?.trim() ?? '';
  return {
    code,
    message: error?.message?.trim() ?? '',
    canConfigure: ['MODEL_AUTH', 'MODEL_NOT_FOUND', 'CONFIG', 'REACT_MAX_ITERS'].includes(code),
  };
}
