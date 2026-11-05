import { request } from '@/services/request';

const DEFAULT_AGENT_ID = 'default';

export interface PoolSkill {
  name: string;
  source: string;
  protected: boolean;
  group: string;
  description: string;
}

export interface AgentSkill {
  name: string;
  enabled: boolean;
  source: string;
  installed_from: string;
  description: string;
}

function skillPath(name: string): string {
  return encodeURIComponent(name);
}

function agentPath(agentId = DEFAULT_AGENT_ID): string {
  return encodeURIComponent(agentId);
}

export const skillHubApi = {
  listPool() {
    return request<{ skills: PoolSkill[] }>('/skills/pool');
  },

  importPool(file: File) {
    const body = new FormData();
    body.append('file', file);
    return request<{ imported: string[] }>('/skills/pool/import', {
      method: 'POST',
      body,
    });
  },

  deletePool(name: string) {
    return request<{ deleted: string }>(`/skills/pool/${skillPath(name)}`, {
      method: 'DELETE',
    });
  },

  listAgent(agentId = DEFAULT_AGENT_ID) {
    return request<{ skills: AgentSkill[] }>(`/skills/agents/${agentPath(agentId)}`);
  },

  installAgent(name: string, agentId = DEFAULT_AGENT_ID) {
    return request<{ installed: string }>(
      `/skills/agents/${agentPath(agentId)}/install`,
      { method: 'POST', body: JSON.stringify({ name }) },
    );
  },

  setAgentEnabled(name: string, enabled: boolean, agentId = DEFAULT_AGENT_ID) {
    const action = enabled ? 'enable' : 'disable';
    return request<{ enabled: boolean; name: string }>(
      `/skills/agents/${agentPath(agentId)}/${skillPath(name)}/${action}`,
      { method: 'POST' },
    );
  },

  uninstallAgent(name: string, agentId = DEFAULT_AGENT_ID) {
    return request<{ uninstalled: string }>(
      `/skills/agents/${agentPath(agentId)}/${skillPath(name)}`,
      { method: 'DELETE' },
    );
  },
};
