import { getHostAuthContext, subscribeHostAuth } from '../../../services/auth';
import { answerUi } from '../api/ui';
import type { ConsoleChat, TraceMessage } from '../api/types';
import { UiExecutor } from './executor';
import { uiRegistry } from './registry';
import { selectPendingUiCalls } from './contracts';

let authRevision = -1;
let sessionId: string | null = null;
let execution: UiExecutor | null = null;
let activity = '';
const activityListeners = new Set<() => void>();
export const getUiActivity = () => activity;
export function subscribeUiActivity(listener: () => void) {
  activityListeners.add(listener);
  return () => {
    activityListeners.delete(listener);
  };
}
function showActivity(message: string) {
  activity = message;
  activityListeners.forEach((listener) => listener());
}

function cancel() {
  execution?.cancel();
  execution = null;
  showActivity('');
}
subscribeHostAuth(() => {
  const auth = getHostAuthContext();
  if (auth.revision === authRevision && auth.ready) return;
  cancel();
  sessionId = null;
  authRevision = auth.revision;
});
export function setUiSession(value: string | null) {
  if (value !== sessionId) {
    cancel();
    sessionId = value;
  }
}

function ensureExecutor(forSession: string): UiExecutor {
  if (sessionId !== forSession) {
    cancel();
    sessionId = forSession;
  }
  if (execution) return execution;
  const revision = getHostAuthContext().revision;
  execution = new UiExecutor({
    current: (call) => sessionId === call.session_id && getHostAuthContext().revision === revision,
    execute: async (call, context) => {
      showActivity('正在处理界面操作…');
      context.signal.addEventListener('abort', () => showActivity('界面操作已取消或超时。'), {
        once: true,
      });
      const result = await uiRegistry.execute(call.name, call.arguments, context);
      showActivity('界面操作已执行，正在回传结果…');
      return result;
    },
    answer: async (call, outcome, signal) => {
      try {
        await answerUi(
          call.session_id,
          call.chat_id,
          { call_id: call.call_id, ...outcome },
          signal,
        );
      } catch {
        if (!signal.aborted) showActivity('界面操作结果回传失败，请查看对话。');
        return;
      }
      if (signal.aborted) return;
      const labels: Record<string, string> = {
        USER_EDIT_CONFLICT: '检测到未保存的修改，请先处理当前草稿。',
        VIEW_STALE: '页面已发生变化，请重新确认操作目标。',
        FORBIDDEN: '当前身份没有目标操作权限。',
        TARGET_NOT_FOUND: '目标已不存在，请重新确认操作目标。',
        MODULE_NOT_READY: '目标页面未就绪，本次操作未完成。',
      };
      showActivity(
        outcome.status === 'executed'
          ? outcome.result.completed_steps.includes('draft_updated')
            ? '草稿已填写，尚未保存。'
            : '界面操作已完成。'
          : (labels[outcome.error.code] ?? '界面操作未完成，请查看对话中的具体结果。'),
      );
    },
  });
  return execution;
}

export function dispatchPendingUi(projection: { chat: ConsoleChat; messages: TraceMessage[] }) {
  if (projection.chat.status !== 'running') return;
  const executor = ensureExecutor(projection.chat.session_id);
  for (const call of selectPendingUiCalls(projection.messages, projection.chat)) {
    void executor.accept(call);
  }
}
