import type { ContextUsage } from '../../api/types.js';
import type { ChatStreamProjection } from './projectStream.js';

/** Validate the wire payload without inventing a capacity for older servers. */
export function readContextUsage(value: ContextUsage): ContextUsage | null {
  const { context_size, used_tokens, trigger_ratio, reserve_ratio, tool_result_limit } = value;
  if (
    !Number.isSafeInteger(context_size) ||
    context_size <= 0 ||
    !Number.isSafeInteger(used_tokens) ||
    used_tokens < 0 ||
    !Number.isFinite(trigger_ratio) ||
    trigger_ratio <= 0 ||
    trigger_ratio > 0.9 ||
    !Number.isFinite(reserve_ratio) ||
    reserve_ratio <= 0 ||
    reserve_ratio >= trigger_ratio ||
    !Number.isSafeInteger(tool_result_limit) ||
    tool_result_limit <= 0
  )
    return null;
  return { context_size, used_tokens, trigger_ratio, reserve_ratio, tool_result_limit };
}

export function selectLatestContextUsage(
  projections: readonly ChatStreamProjection[],
  sessionId: string | undefined,
): ContextUsage | null {
  let latest: ChatStreamProjection | undefined;
  for (const projection of projections) {
    if (
      projection.chat.session_id === sessionId &&
      projection.contextUsage &&
      (!latest || projection.chat.sequence > latest.chat.sequence)
    )
      latest = projection;
  }
  return latest?.contextUsage ?? null;
}

export function contextUsagePercent(usage: ContextUsage): number {
  return (usage.used_tokens / usage.context_size) * 100;
}

export function contextCompressionProgress(usage: ContextUsage): number {
  return Math.min(100, (usage.used_tokens / (usage.context_size * usage.trigger_ratio)) * 100);
}

export function minimumTriggerPercent(reserveRatio: number): number {
  const minimum = Math.max(1, Math.floor(reserveRatio * 100) + 1);
  return minimum / 100 <= reserveRatio ? minimum + 1 : minimum;
}

export function formatContextTokens(tokens: number): string {
  if (tokens < 1000) return String(tokens);
  const divisor = tokens >= 1_000_000 ? 1_000_000 : 1000;
  return `${Number((tokens / divisor).toFixed(1))}${divisor === 1000 ? 'K' : 'M'}`;
}
