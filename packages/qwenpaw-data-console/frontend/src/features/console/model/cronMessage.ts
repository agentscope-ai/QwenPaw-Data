/** Build an Agent cron prompt from the user query and optional plan steps. */
export function buildCronMessage(query: string, planSteps: string[] = []): string {
  const normalizedQuery = query.trim();
  const steps = planSteps.map((step) => step.trim()).filter(Boolean);
  if (!normalizedQuery && steps.length === 0) return '';
  if (steps.length === 0) return normalizedQuery;

  const planBlock = [
    '参考执行步骤：',
    ...steps.map((subject, index) => `${index + 1}. ${subject}`),
  ].join('\n');
  return normalizedQuery ? `${normalizedQuery}\n\n${planBlock}` : planBlock;
}

export function buildCronJobName(
  query: string,
  fallback = '定时分析任务',
): string {
  const normalizedQuery = query.trim().replace(/\s+/g, ' ');
  if (!normalizedQuery) return fallback;
  const truncated = normalizedQuery.length > 40
    ? `${normalizedQuery.slice(0, 40)}…`
    : normalizedQuery;
  return `定时分析：${truncated}`;
}
