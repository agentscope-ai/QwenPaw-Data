import { request } from '@/services/request';

export type McpConfigType = 'stdio_mcp' | 'http_mcp';

export interface McpConfig {
  type: McpConfigType;
  command?: string;
  args?: string[] | null;
  env?: Record<string, string> | null;
  cwd?: string | null;
  url?: string;
  headers?: Record<string, string> | null;
  timeout?: number | null;
}

export interface McpClient {
  name: string;
  display_name: string;
  description: string;
  enabled: boolean;
  builtin: boolean;
  is_stateful: boolean;
  mcp_config: McpConfig;
  enable_tools: string[] | null;
}

export interface McpTool {
  name: string;
  description: string;
  enabled: boolean;
  input_schema: Record<string, unknown>;
}

export interface McpClientWrite {
  name: string;
  display_name?: string;
  description?: string;
  enabled?: boolean;
  is_stateful?: boolean;
  mcp_config: McpConfig;
  enable_tools?: string[] | null;
}

function mcpPath(name: string): string {
  return encodeURIComponent(name);
}

export const mcpHubApi = {
  list() {
    return request<{ clients: McpClient[] }>('/mcp');
  },

  create(body: McpClientWrite) {
    return request<{ client: McpClient }>('/mcp', {
      method: 'POST',
      body: JSON.stringify(body),
    });
  },

  update(name: string, body: McpClientWrite) {
    return request<{ client: McpClient }>(`/mcp/${mcpPath(name)}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    });
  },

  remove(name: string) {
    return request<{ deleted: string }>(`/mcp/${mcpPath(name)}`, {
      method: 'DELETE',
    });
  },

  setEnabled(name: string, enabled: boolean) {
    const action = enabled ? 'enable' : 'disable';
    return request<{ client: McpClient }>(`/mcp/${mcpPath(name)}/${action}`, {
      method: 'POST',
    });
  },

  importConfig(config: unknown) {
    return request<{ clients: McpClient[] }>('/mcp/import', {
      method: 'POST',
      body: JSON.stringify({ config }),
    });
  },

  listTools(name: string) {
    return request<{ tools: McpTool[] }>(`/mcp/${mcpPath(name)}/tools`);
  },

  setTools(name: string, enableTools: string[] | null) {
    return request<{ client: McpClient }>(`/mcp/${mcpPath(name)}/tools`, {
      method: 'PUT',
      body: JSON.stringify({ enable_tools: enableTools }),
    });
  },
};
