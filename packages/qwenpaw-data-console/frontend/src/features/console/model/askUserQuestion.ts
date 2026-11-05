import type {
  AskUserQuestionOption,
  AskUserQuestionQuestion,
  PendingAskUserQuestion,
  TraceMessage,
} from '../api/types';

interface ToolCallData {
  call_id?: unknown;
  name?: unknown;
  arguments?: unknown;
  output?: unknown;
}

function parseArguments(value: unknown): unknown {
  if (typeof value !== 'string') return value;
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}

export function parseAskUserQuestion(
  message: TraceMessage,
): PendingAskUserQuestion | null {
  if (message.type !== 'plugin_call' || message.status !== 'completed') return null;
  const content = message.content.find((item) => item.type === 'data');
  const data = content?.data as ToolCallData | null;
  if (!data || data.name !== 'ask_user_question' || typeof data.call_id !== 'string') {
    return null;
  }
  const input = parseArguments(data.arguments) as Record<string, unknown> | null;
  if (!input || typeof input.title !== 'string' || !Array.isArray(input.questions)) {
    return null;
  }
  const questions = input.questions.filter((question): question is Record<string, unknown> => {
    if (!question || typeof question !== 'object') return false;
    const item = question as Record<string, unknown>;
    if (
      typeof item.question !== 'string'
      || (item.description !== undefined && typeof item.description !== 'string')
      || typeof item.multiSelect !== 'boolean'
      || !Array.isArray(item.options)
      || item.options.length < 2
      || item.options.length > 4
    ) {
      return false;
    }
    return item.options.every((option) => {
      if (!option || typeof option !== 'object') return false;
      const candidate = option as Record<string, unknown>;
      return typeof candidate.label === 'string'
        && (
          candidate.description === undefined
          || candidate.description === null
          || typeof candidate.description === 'string'
        );
    });
  });
  if (questions.length !== input.questions.length || !questions.length) return null;

  const normalizedQuestions: AskUserQuestionQuestion[] = questions.map((question) => ({
    question: question.question as string,
    description: typeof question.description === 'string' ? question.description : '',
    multiSelect: question.multiSelect as boolean,
    options: (question.options as Record<string, unknown>[]).map<AskUserQuestionOption>((option) => ({
      label: option.label as string,
      description: typeof option.description === 'string' ? option.description : null,
    })),
    required: true,
    allowCustomText: true,
  }));

  return {
    id: data.call_id,
    chat_id: message.chat_id,
    title: input.title,
    questions: normalizedQuestions,
    expires_at: typeof message.metadata?.expires_at === 'string'
      ? message.metadata.expires_at
      : null,
  };
}

function messageCallId(message: TraceMessage): string | null {
  const content = message.content.find((item) => item.type === 'data');
  const data = content?.data as ToolCallData | null;
  return typeof data?.call_id === 'string' ? data.call_id : message.source_id ?? null;
}

function isResolvedToolResult(message: TraceMessage): boolean {
  if (message.type !== 'plugin_call_output' || message.status !== 'completed') return false;
  const content = message.content.find((item) => item.type === 'data');
  const data = content?.data as ToolCallData | null;
  if (!data) return false;
  const output = parseArguments(data.output) as Record<string, unknown> | null;
  return output?.status === 'answered' || output?.status === 'timeout';
}

export function isAskUserQuestionExpired(
  clarification: PendingAskUserQuestion,
  now = Date.now(),
): boolean {
  if (!clarification.expires_at) return false;
  const deadline = Date.parse(clarification.expires_at);
  return !Number.isNaN(deadline) && deadline <= now;
}

export function selectPendingAskUserQuestion(
  messages: TraceMessage[],
): PendingAskUserQuestion | null {
  const resolvedIds = new Set(
    messages
      .filter(isResolvedToolResult)
      .map(messageCallId)
      .filter((id): id is string => Boolean(id)),
  );
  return [...messages]
    .sort((left, right) => right.sequence - left.sequence)
    .map(parseAskUserQuestion)
    .find((item) => item !== null && !resolvedIds.has(item.id)) ?? null;
}
