export type UsagePeriod = '1d' | '7d' | '30d';

export interface UsageQuery {
  period: UsagePeriod; // 服务端自然日窗口，默认页面选用 7d。
  timezone?: string; // IANA 时区；后续请求复用 Summary 返回值。
  as_of?: string; // 服务端 RFC3339 快照时间，不使用客户端时间生成。
}

export interface UsagePageQuery extends UsageQuery {
  page: number; // 从 1 开始的独立明细页码。
  page_size: number; // 每页聚合行数，后端允许 1 至 100。
}

export interface UsageWindow {
  period: UsagePeriod; // 服务端实际使用的窗口类型。
  start: string; // 窗口起点，包含该时刻。
  end: string; // 窗口终点，不包含该时刻。
  timezone: string; // 分桶和日期展示使用的 IANA 时区。
  as_of: string; // 四个区域及后续翻页共用的服务端快照。
}

export interface GenerationUsage {
  input_tokens: number; // 生成模型输入 Token 数。
  output_tokens: number; // 生成模型输出 Token 数。
  total_tokens: number; // 生成输入与输出 Token 合计。
  calls: number; // 可观察的生成模型物理调用次数。
}

export interface EmbeddingUsage {
  tokens: number; // Embedding 输入 Token 数。
  calls: number; // 可观察的 Embedding 物理调用次数。
}

export interface UsageTotals {
  generation: GenerationUsage; // 全窗口生成用量，不是当前页累加。
  embedding: EmbeddingUsage; // 全窗口 Embedding 用量。
  total_tokens: number; // 生成与 Embedding Token 合计。
}

export interface UsageModel {
  provider_id: string; // 实际调用的供应商标识。
  model_id: string; // 实际调用的模型标识。
  model_type: 'text' | 'embedding'; // 后端将 chat 类型归一为 text。
}

export interface UsagePagination {
  page: number; // 当前响应的页码。
  page_size: number; // 当前响应的每页行数。
  total: number; // 所有匹配聚合行数。
  page_count: number; // 总页数，无数据时为 0。
  has_previous: boolean; // 是否存在前一页。
  has_next: boolean; // 是否存在后一页。
}

export interface UsageTimeseriesPoint {
  bucket_start: string; // 服务端时间桶起点。
  bucket_end: string; // 桶终点可能晚于快照，前端不再裁剪或重分桶。
  label: string; // 服务端提供的横轴标签。
  is_partial: boolean; // 当前尚未结束的时间桶标记。
  generation: GenerationUsage; // 当前桶的生成用量。
  embedding: EmbeddingUsage; // 当前桶的 Embedding 用量。
}

export interface SessionUsageItem extends UsageTotals {
  session_id: string; // Session 标识，也是表格稳定行键。
  title: string; // 后端已处理无标题时的回退文案。
  session_state: 'active' | 'deleted'; // 软删除后仍保留统计。
  last_activity_at: string; // 当前窗口内最近调用完成时间。
  models: UsageModel[]; // 按供应商、模型和类型去重的实际调用模型。
}

export interface BackgroundTaskUsageItem extends UsageTotals {
  group_id: string; // source 聚合的稳定行键，不是任务 ID。
  source: string; // 原样展示的任务来源。
  last_activity_at: string; // 当前窗口内最近调用完成时间。
  models: UsageModel[]; // 本来源的实际调用模型。
}

export interface UsageSummary extends UsageTotals {
  window: UsageWindow; // 本页其余三个查询共用的窗口与快照。
  sessions: {
    count: number; // 全窗口内可归属的 Session 数量。
  }; // Session 聚合数量。
  background_tasks: {
    count: number; // 全窗口后台来源分组数，不是任务执行次数。
  }; // 后台来源聚合数量。
}

export interface UsageTimeseries {
  window: UsageWindow; // 与 Summary 相同的窗口与快照。
  granularity: 'hour4' | 'day'; // 1d 按四小时，7d/30d 按自然日。
  points: UsageTimeseriesPoint[]; // 服务端已补零且排序的时间桶。
  totals: UsageTotals; // 全窗口趋势合计。
}

export interface SessionUsageTotals extends UsageTotals {
  sessions: number; // 全窗口 Session 数量，不是当前页行数。
}

export interface BackgroundTaskUsageTotals extends UsageTotals {
  groups: number; // 全窗口来源分组数，不是当前页行数。
}

export interface SessionUsagePage {
  window: UsageWindow; // 本次明细查询的窗口与快照。
  items: SessionUsageItem[]; // 按服务端最近活动顺序排列的当前页。
  totals: SessionUsageTotals; // 全窗口所有匹配 Session 的合计。
  pagination: UsagePagination; // Session 独立分页信息。
}

export interface BackgroundTaskUsagePage {
  window: UsageWindow; // 本次明细查询的窗口与快照。
  items: BackgroundTaskUsageItem[]; // 按服务端总 Token 等规则排序的当前页。
  totals: BackgroundTaskUsageTotals; // 全窗口所有来源的合计。
  pagination: UsagePagination; // 后台来源独立分页信息。
}
