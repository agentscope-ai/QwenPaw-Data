import type { UsageSummary } from '../../api/usageTypes.js';

export function formatUsageNumber(value: number, language: string, compact = true): string {
  const locale = language === 'zh' ? 'zh-CN' : 'en-US';
  const magnitude = Math.abs(value);
  const unit = magnitude >= 1_000_000 ? 1_000_000 : magnitude >= 1_000 ? 1_000 : 1;
  if (!compact || unit === 1)
    return new Intl.NumberFormat(locale, { maximumFractionDigits: 0 }).format(value);
  return `${new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(value / unit)}${unit === 1_000 ? 'K' : 'M'}`;
}

export function formatUsageDate(value: string, timezone: string, language: string): string {
  return new Intl.DateTimeFormat(language === 'zh' ? 'zh-CN' : 'en-GB', {
    timeZone: timezone,
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).format(new Date(value));
}

export function isUsageEmpty(summary: UsageSummary): boolean {
  return summary.generation.calls === 0 && summary.embedding.calls === 0;
}
