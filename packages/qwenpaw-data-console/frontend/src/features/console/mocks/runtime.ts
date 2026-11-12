import type {
  IAgentScopeRuntimeWebUIMessage,
  IAgentScopeRuntimeWebUISession,
  IAgentScopeRuntimeWebUISessionAPI,
} from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/types';
import type { IAgentScopeRuntimeMessage } from '@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/types';
import type { Segment } from '../api/types';

const completed = 'completed' as const;
const inProgress = 'in_progress' as const;

export const mockSegments: Segment[] = [
  {
    segment_id: 'seg_chat02_01',
    chat_id: 'chat-02',
    title: '确认指标口径与分析范围',
    input: '通义业务库、全站 DAU、2025 年 12 月自然月',
    behavior: '采用日粒度统计，国家维度使用注册地区，排除内部测试账号。',
    conclusion: '已确认指标口径与时间范围，可进入趋势和归因分析。',
    artifact: null,
    coverage: { start_seq: 1, end_seq: 4 },
    started_at: 1,
    ended_at: 2,
  },
  {
    segment_id: 'seg_chat02_02',
    chat_id: 'chat-02',
    title: '定位印度市场的变化时间点',
    input: '全站 DAU、印度、2025-12-01 至 2025-12-31',
    behavior: '采用 7 日均线与变化点检测，排除周末周期影响。',
    conclusion: '12 月 12 日后 DAU 持续下移，后 7 日均值较前 7 日下降 18.7%。',
    artifact: [
      {
        name: 'india_dau_change_point.sql',
        description: '印度 DAU 变化点查询',
        relative_path: 'analysis/india_dau_change_point.sql',
      },
      {
        name: 'india_dau_change_point.csv',
        description: '印度 DAU 变化点结果集',
        relative_path: 'analysis/india_dau_change_point.csv',
      },
    ],
    coverage: { start_seq: 5, end_seq: 10 },
    started_at: 3,
    ended_at: 4,
  },
];

const requestCard = (id: string, query: string): IAgentScopeRuntimeWebUIMessage => ({
  id,
  role: 'user',
  msgStatus: 'finished',
  cards: [
    {
      code: 'AgentScopeRuntimeRequestCard',
      data: {
        input: [
          {
            role: 'user',
            type: 'message',
            content: [{ type: 'text', text: query, status: 'created' }],
          },
        ],
      },
    },
  ],
});

const textMessage = (id: string, text: string): IAgentScopeRuntimeMessage => ({
  id,
  object: 'message' as const,
  role: 'assistant',
  type: 'message',
  content: [{ type: 'text', text, status: completed }],
  status: completed,
} as IAgentScopeRuntimeMessage);

const reasoningMessage = (id: string, text: string): IAgentScopeRuntimeMessage => ({
  id,
  object: 'message' as const,
  role: 'assistant',
  type: 'reasoning',
  content: [{ type: 'text', text, status: inProgress }],
  status: inProgress,
} as IAgentScopeRuntimeMessage);

const toolCall = (
  id: string,
  name: string,
  args: Record<string, unknown>,
  status: typeof completed | typeof inProgress = completed,
): IAgentScopeRuntimeMessage => ({
  id,
  object: 'message' as const,
  role: 'assistant',
  type: 'tool_call',
  content: [
    {
      type: 'data',
      data: {
        call_id: id,
        name,
        arguments: JSON.stringify(args),
      },
      status,
    },
  ],
  status,
} as IAgentScopeRuntimeMessage);

const toolOutput = (
  id: string,
  callId: string,
  name: string,
  output: unknown,
): IAgentScopeRuntimeMessage => ({
  id,
  object: 'message' as const,
  role: 'tool',
  type: 'tool_call_output',
  content: [
    {
      type: 'data',
      data: {
        call_id: callId,
        name,
        output: typeof output === 'string' ? output : JSON.stringify(output),
      },
      status: completed,
    },
  ],
  status: completed,
} as IAgentScopeRuntimeMessage);

const responseCard = (
  id: string,
  output: IAgentScopeRuntimeMessage[],
  status: typeof completed | typeof inProgress,
): IAgentScopeRuntimeWebUIMessage => ({
  id,
  role: 'assistant',
  msgStatus: status === completed ? 'finished' : 'generating',
  cards: [
    {
      code: 'AgentScopeRuntimeResponseCard',
      data: {
        id: `response_${id}`,
        object: 'response',
        status,
        created_at: Date.now(),
        completed_at: status === completed ? Date.now() : undefined,
        output,
      },
    },
  ],
});

const completedOutput = [
  textMessage(
    'msg_summary',
    '已完成 12 月全站 DAU 趋势分析。整体在月中出现结构性下移，印度是主要下降贡献市场，俄罗斯的增长抵消了部分降幅。',
  ),
  toolCall('call_metric', 'GetMetric', { metric_id: 'dau_001' }),
  toolOutput('out_metric', 'call_metric', 'GetMetric', {
    display_name: '全站 DAU',
    aggregation: 'count_distinct(user_id)',
  }),
  toolCall('call_sql', 'ExecuteSQL', {
    datasource_id: 'sales_business_db',
    statement: 'SELECT dt, country, dau FROM example_app_dau_daily ...',
  }),
  toolOutput('out_sql', 'call_sql', 'ExecuteSQL', {
    rows: 31,
    elapsed_ms: 3720,
    cache_hit: false,
  }),
  toolCall('call_artifact', 'register_artifact', {
    name: 'india_dau_change_point.sql',
    path: '/artifacts/india_dau_change_point.sql',
    mime_type: 'text/sql',
    segment_id: 'seg_chat02_02',
  }),
  toolOutput('out_artifact', 'call_artifact', 'register_artifact', {
    artifact_id: 'artifact-change-point-sql',
  }),
];

const runningOutput = [
  reasoningMessage(
    'msg_reasoning',
    '先固定指标口径，再按渠道、客户端版本和用户类型构造交叉贡献矩阵。',
  ),
  toolCall('call_dimension', 'GetDimension', {
    dataset_id: 'example_app_daily_activity',
    dimensions: ['channel', 'app_version', 'user_type'],
  }),
  toolOutput('out_dimension', 'call_dimension', 'GetDimension', {
    accepted_dimensions: ['channel', 'app_version', 'user_type'],
  }),
  toolCall(
    'call_attribution',
    'ExecuteSQL',
    {
      datasource_id: 'sales_business_db',
      statement: 'SELECT channel, app_version, user_type, contribution ...',
    },
    inProgress,
  ),
];

const initialMessages: IAgentScopeRuntimeWebUIMessage[] = [
  requestCard(
    'runtime-user-chat02',
    '分析一下 ExampleApp 今年 12 月的访问趋势，重点看看异常日期和国家贡献。',
  ),
  responseCard('runtime-assistant-chat02', completedOutput, completed),
  requestCard(
    'runtime-user-chat04',
    '继续定位印度 DAU 下滑的核心驱动因素，按渠道、版本和用户类型拆解。',
  ),
  responseCard('runtime-assistant-chat04', runningOutput, inProgress),
];

const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T;

export function createMockRuntimeSessionApi(): IAgentScopeRuntimeWebUISessionAPI {
  let session: IAgentScopeRuntimeWebUISession = {
    id: 'session-example_app-december',
    name: 'ExampleApp 12 月访问趋势与印度市场归因',
    messages: clone(initialMessages),
    generating: false,
  };

  return {
    getSessionList: async () => [clone(session)],
    getSession: async () => clone(session),
    updateSession: async (patch) => {
      session = { ...session, ...clone(patch) };
      return [clone(session)];
    },
    createSession: async (patch) => {
      session = {
        id: patch.id ?? `session-${Date.now()}`,
        name: patch.name ?? '新建数据会话',
        messages: patch.messages ?? [],
        generating: patch.generating,
      };
      return [clone(session)];
    },
    removeSession: async () => [clone(session)],
  };
}

export function createMockRuntimeResponse(): Response {
  const now = Date.now();
  const payload = {
    id: `response_mock_${now}`,
    object: 'response',
    status: completed,
    created_at: now,
    completed_at: now,
    output: [
      textMessage(
        `message_mock_${now}`,
        '已接收补充要求。',
      ),
    ],
  };

  return new Response(`data: ${JSON.stringify(payload)}\n\n`, {
    status: 200,
    headers: {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
    },
  });
}
