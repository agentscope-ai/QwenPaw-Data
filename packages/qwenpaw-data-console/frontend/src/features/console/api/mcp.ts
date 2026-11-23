import { request } from '@/services/request';

export type McpSource = 'builtin' | 'custom';

export interface HttpMcpConfig {
  type: 'http_mcp';
  url: string;
  headers?: Record<string, string> | null;
  timeout?: number | null;
}

export interface StdioMcpConfig {
  type: 'stdio_mcp';
  command: string;
  args?: string[] | null;
  env?: Record<string, string> | null;
  cwd?: string | null;
}

export type McpConfig = HttpMcpConfig | StdioMcpConfig;

export interface McpItem {
  name: string;
  enabled: boolean;
  source: string;
  description: string;
  is_stateful: boolean;
  mcp_config: McpConfig;
  enable_tools: string[] | null;
  disable_tools: string[] | null;
  execution_timeout: number | null;
}

export interface McpWriteBody {
  name: string;
  description: string;
  enabled: boolean;
  source: McpSource;
  is_stateful: boolean;
  mcp_config: McpConfig;
  enable_tools: string[] | null;
  disable_tools: string[] | null;
  execution_timeout: number | null;
}

export interface McpTool {
  name: string;
  description: string;
  enabled: boolean;
  input_schema: Record<string, unknown>;
}

function mcpPath(name: string): string {
  return encodeURIComponent(name);
}

export function createMcpApi(agentId = 'default') {
  const scopedRequest: typeof request = (path, options) =>
    request(`${path}?agent_id=${encodeURIComponent(agentId)}`, options);
  return {
  list() {
    return scopedRequest<{ mcps: McpItem[] }>('/mcp');
  },

  get(name: string) {
    return scopedRequest<McpItem>(`/mcp/${mcpPath(name)}`);
  },

  create(body: McpWriteBody) {
    return scopedRequest<McpItem>('/mcp', {
      method: 'POST',
      body: JSON.stringify(body),
    });
  },

  update(name: string, body: McpWriteBody) {
    return scopedRequest<McpItem>(`/mcp/${mcpPath(name)}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    });
  },

  delete(name: string) {
    return scopedRequest<{ deleted: string }>(`/mcp/${mcpPath(name)}`, {
      method: 'DELETE',
    });
  },

  enable(name: string) {
    return scopedRequest<McpItem>(`/mcp/${mcpPath(name)}/enable`, { method: 'POST' });
  },

  disable(name: string) {
    return scopedRequest<McpItem>(`/mcp/${mcpPath(name)}/disable`, { method: 'POST' });
  },

  listTools(name: string) {
    return scopedRequest<{ tools: McpTool[] }>(`/mcp/${mcpPath(name)}/tools`);
  },

  setTools(name: string, enable_tools: string[] | null, disable_tools: string[] | null) {
    return scopedRequest<McpItem>(`/mcp/${mcpPath(name)}/tools`, {
      method: 'PUT',
      body: JSON.stringify({ enable_tools, disable_tools }),
    });
  },
};
}

export const mcpApi = createMcpApi();
