import type { Artifact, ConversationTurn, PlanStep, TraceStep } from '../types';

export const conversationTurns: ConversationTurn[] = [
  {
    id: 'chat-01',
    number: '01',
    title: '确认访问情况的指标口径',
    summary: '已确认使用全站 DAU 与自然月范围',
    dataSource: '通义业务库',
    sentAt: '10:31',
    status: 'awaiting',
  },
  {
    id: 'chat-02',
    number: '02',
    title: '分析 ExampleApp 12 月访问趋势',
    summary: '识别 7 个异常日及主要国家贡献',
    dataSource: '通义业务库',
    sentAt: '10:33',
    status: 'done',
    duration: '42.8s',
  },
  {
    id: 'chat-03',
    number: '03',
    title: '对比俄罗斯 11 月与 12 月 DAU',
    summary: '12 月日均 DAU 环比增长 12.4%',
    dataSource: '通义业务库',
    sentAt: '10:35',
    status: 'done',
    duration: '8.6s',
  },
  {
    id: 'chat-04',
    number: '04',
    title: '定位印度 DAU 下滑的核心驱动因素',
    summary: '正在分析渠道、版本与用户类型的交叉贡献',
    dataSource: '通义业务库',
    sentAt: '10:38',
    status: 'running',
  },
];

export const planSteps: PlanStep[] = [
  { id: '01', title: '复用并校验已确认的数据口径', status: 'done' },
  { id: '02', title: '定位印度市场的变化时间点', status: 'done' },
  { id: '03', title: '拆解渠道、版本与用户类型贡献', status: 'running' },
  { id: '04', title: '验证维度交叉影响与异常样本', status: 'todo' },
  { id: '05', title: '形成结论与可执行建议', status: 'todo' },
];

export const artifacts: Artifact[] = [
  {
    id: 'artifact-sql',
    name: 'india_dau_attribution.sql',
    title: '印度 DAU 归因查询',
    type: 'SQL',
    meta: '46 行 · MaxCompute · Chat 04',
    important: true,
  },
  {
    id: 'artifact-csv',
    name: 'country_contribution.csv',
    title: '国家贡献度结果集',
    type: 'CSV',
    meta: '18 行 · 5 个字段 · Chat 02',
    important: true,
  },
  {
    id: 'artifact-html',
    name: 'dau_diagnostics_report.html',
    title: 'DAU 趋势诊断报告',
    type: 'HTML',
    meta: '交互报告 · Chat 02',
  },
  {
    id: 'artifact-png',
    name: 'example_app_dec_dau_trend.png',
    title: '12 月 DAU 趋势图',
    type: 'PNG',
    meta: '1600 × 900 · Chat 02',
  },
];

export const businessTrace: TraceStep[] = [
  {
    id: 'trace-1',
    title: '读取已确认的全站 DAU 口径',
    detail: '使用指标「全站 DAU」，限定印度市场与 2025 年 12 月。',
    tool: '语义检索',
    status: 'done',
  },
  {
    id: 'trace-2',
    title: '完成趋势变化点检测',
    detail: '确认 12 月 12 日后出现持续性下移，并排除节假日季节性。',
    tool: '趋势诊断',
    status: 'done',
  },
  {
    id: 'trace-3',
    title: '计算渠道、版本与用户类型贡献',
    detail: '正在执行三维交叉归因，当前已完成 68% 的候选组合。',
    tool: '归因分析',
    status: 'running',
  },
];

export const rawTrace: TraceStep[] = [
  {
    id: 'raw-1',
    title: 'search_metric_graph',
    detail: 'metric_id=dau_001, datasource=production_warehouse',
    tool: 'Tool',
    status: 'done',
  },
  {
    id: 'raw-2',
    title: 'ExecuteSQL',
    detail: 'rows=31, elapsed=3.7s, cache_hit=false',
    tool: 'Tool',
    status: 'done',
  },
  {
    id: 'raw-3',
    title: 'ExecuteShellCMD',
    detail: 'python attribution.py --dimensions channel,version,user_type',
    tool: 'Tool',
    status: 'running',
  },
];

export const artifactRows = [
  { key: '1', dimension: '自然流量', first: '84,230', last: '68,910', delta: '-15,320', contribution: '41.8%' },
  { key: '2', dimension: 'Android 3.5.1', first: '51,420', last: '40,870', delta: '-10,550', contribution: '28.8%' },
  { key: '3', dimension: '新用户', first: '37,180', last: '30,040', delta: '-7,140', contribution: '19.5%' },
];
