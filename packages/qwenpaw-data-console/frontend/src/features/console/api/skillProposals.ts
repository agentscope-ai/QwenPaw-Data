import { request } from '@/services/request';

export type SkillProposalStatus = 'pending' | 'accepted' | 'partial' | 'rejected';

export interface SkillHunk {
  hunk_id: string; // 后端生成的 hunk 唯一标识。
  header: string; // 审阅页展示的编辑位置摘要。
  old_start: number; // 修改前展示范围的起始行（含上下文），从 1 开始，空范围为 0。
  old_count: number; // 修改前展示范围的行数（含上下文）。
  new_start: number; // 修改后展示范围的起始行（含上下文），从 1 开始，空范围为 0。
  new_count: number; // 修改后展示范围的行数（含上下文）。
  old_string?: string; // 接受时用于匹配的原文。
  new_string?: string; // 接受后写入的新文本。
  reason?: string; // 产生该编辑的原因。
  diff?: string; // 带上下文的 hunk diff。
  status?: string; // pending / accepted / rejected。
}

export interface SkillFileChange {
  patch_id: string; // 文件级 patch 唯一标识。
  skill_name: string; // 被修改的 Skill 名称。
  relative_path: string; // Skill 目录内的相对文件路径。
  before?: string; // 修改前的文件全文。
  after?: string; // 修改后的文件全文。
  diff?: string; // 文件级 unified diff。
  reason?: string; // 文件级修改原因。
  status?: string; // pending / accepted / rejected。
  hunks?: SkillHunk[]; // 文件内可独立选择的编辑项。
}

export interface SkillProposal {
  id: string; // Host 生成的提案 ID。
  user_id: string; // 提案所属用户 ID。
  tenant_id: string; // 提案所属租户 ID。
  workspace_id: string; // 提案所属工作空间 ID。
  session_id: string; // 提案所属会话 ID，详情及写操作必须使用此值。
  agent_id: string; // 提案最终写入的 Agent ID。
  status: SkillProposalStatus | string; // 提案处理状态。
  summary: string; // 列表及审阅页的一句话摘要。
  mentioned_skill_names: string[]; // 本提案涉及的 Skill 名称。
  modified_file_count: number; // 本提案涉及的文件数。
  file_changes?: SkillFileChange[]; // 当前 Host 详情响应使用的文件字段。
  patches?: SkillFileChange[]; // PRD 详情响应约定的兼容字段。
  created_at?: string | null; // 创建时间。
  updated_at?: string | null; // 最后更新时间。
}

export interface SkillProposalAcceptSelection {
  patch_ids?: string[] | null; // 接受指定文件下的全部 hunk。
  hunk_ids?: string[] | null; // 接受指定 hunk，优先级高于 patch_ids。
}

interface SkillProposalsResponse {
  skill_proposals: SkillProposal[]; // 提案摘要列表，不包含 file_changes。
  count: number; // 列表条目总数。
}

interface SkillProposalDetailResponse {
  skill_proposal: SkillProposal; // 含文件变更和 hunk 的提案详情。
}

interface SkillProposalActionResponse {
  ok: boolean; // 本次提案操作是否成功。
  skill_proposal: SkillProposal; // 操作后的提案。
}

function skillProposalsPath(sessionId: string): string {
  return `/sessions/${encodeURIComponent(sessionId)}/settlement/skill-proposals`;
}

export const skillProposalApi = {
  listWorkspace() {
    // 身份和工作空间由登录上下文确定；不传 status，交由页面分组。
    return request<SkillProposalsResponse>('/settlement/skill-proposals');
  },

  list(sessionId: string, status?: SkillProposalStatus) {
    const query = status ? `?status=${encodeURIComponent(status)}` : '';
    return request<SkillProposalsResponse>(`${skillProposalsPath(sessionId)}${query}`);
  },

  get(sessionId: string, proposalId: string) {
    return request<SkillProposalDetailResponse>(
      `${skillProposalsPath(sessionId)}/${encodeURIComponent(proposalId)}`,
    );
  },

  accept(sessionId: string, proposalId: string, body: SkillProposalAcceptSelection = {}) {
    return request<SkillProposalActionResponse>(
      `${skillProposalsPath(sessionId)}/${encodeURIComponent(proposalId)}/accept`,
      {
        method: 'POST',
        body: JSON.stringify(body),
      },
    );
  },

  reject(sessionId: string, proposalId: string) {
    return request<SkillProposalActionResponse>(
      `${skillProposalsPath(sessionId)}/${encodeURIComponent(proposalId)}/reject`,
      { method: 'POST' },
    );
  },
};
