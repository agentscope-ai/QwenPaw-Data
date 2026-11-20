import { request } from '@/services/request';

const DEFAULT_AGENT_ID = 'default';

export interface AgentSkill {
  name: string;
  enabled: boolean;
  source: string;
  group: string;
  description: string;
}

export interface AgentSkillDetail extends AgentSkill {
  content: string;
}

export interface SkillRevision {
  revision_no: number;
  origin: string;
  source_ref: string;
  note: string;
  created_at: string;
}

export interface SkillRevisionFile {
  relative_path: string;
  content: string;
}

export interface SkillRevisionDetail extends SkillRevision {
  files: SkillRevisionFile[];
}

function skillPath(name: string): string {
  return encodeURIComponent(name);
}

function agentPath(agentId = DEFAULT_AGENT_ID): string {
  return encodeURIComponent(agentId);
}

export const skillHubApi = {
  listAgent(agentId = DEFAULT_AGENT_ID) {
    return request<{ skills: AgentSkill[] }>(`/skills/agents/${agentPath(agentId)}`);
  },

  getAgent(name: string, agentId = DEFAULT_AGENT_ID, signal?: AbortSignal) {
    return request<AgentSkillDetail>(`/skills/agents/${agentPath(agentId)}/${skillPath(name)}`, {
      signal,
    });
  },

  importAgent(file: File, agentId = DEFAULT_AGENT_ID) {
    const body = new FormData();
    body.append('file', file);
    return request<{ imported: string[] }>(`/skills/agents/${agentPath(agentId)}/import`, {
      method: 'POST',
      body,
    });
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

  createAgent(name: string, content: string, agentId = DEFAULT_AGENT_ID) {
    return request<{ created: string }>(`/skills/agents/${agentPath(agentId)}`, {
      method: 'POST',
      body: JSON.stringify({ name, content }),
    });
  },

  saveAgent(name: string, content: string, baseContent: string, agentId = DEFAULT_AGENT_ID) {
    return request<{ saved: string }>(`/skills/agents/${agentPath(agentId)}/${skillPath(name)}`, {
      method: 'PUT',
      body: JSON.stringify({
        name,
        content,
        base_content: baseContent,
      }),
    });
  },

  listRevisions(name: string, agentId = DEFAULT_AGENT_ID, signal?: AbortSignal) {
    return request<{ revisions: SkillRevision[]; count: number }>(
      `/skills/agents/${agentPath(agentId)}/${skillPath(name)}/revisions`,
      { signal },
    );
  },

  getRevision(name: string, revisionNo: number, agentId = DEFAULT_AGENT_ID, signal?: AbortSignal) {
    return request<SkillRevisionDetail>(
      `/skills/agents/${agentPath(agentId)}/${skillPath(name)}/revisions/${revisionNo}`,
      { signal },
    );
  },

  revertRevision(name: string, revisionNo: number, agentId = DEFAULT_AGENT_ID) {
    return request<{ ok: boolean; revision: SkillRevision }>(
      `/skills/agents/${agentPath(agentId)}/${skillPath(name)}/revisions/${revisionNo}/revert`,
      { method: 'POST' },
    );
  },
};
