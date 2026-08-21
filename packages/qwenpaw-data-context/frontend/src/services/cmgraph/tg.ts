// ============================================================
// TG (Trace Graph) — 执行链管理模块
// Covers: Task, Claim, Strategy Card, Tag
// ============================================================

import { cmRequest, cmRequestWithMeta, encodeKey, buildPageQuery } from "./helpers";
import type {
  GraphData,
  PaginationMeta,
  TaskListItem,
  TaskListParams,
  TaskStatusUpdatePayload,
  TaskBatchArchivePayload,
  TaskBatchDeletePayload,
  ClaimListItem,
  ClaimListParams,
  ClaimUpdatePayload,
  ClaimInvalidatePayload,
  StrategyCard,
  StrategyListParams,
  StrategyUpdatePayload,
  StrategyInvalidatePayload,
  TagListItem,
  TagListParams,
} from "./types";

// ------------------------------------------------------------
// Task Management
// ------------------------------------------------------------

/** GET /tg/tasks — 获取 Task 列表（支持 status, date, search 过滤 + 分页） */
async function listTasks(
  params: TaskListParams = {},
): Promise<{ data: TaskListItem[]; meta: PaginationMeta | null }> {
  const qs = buildPageQuery(params as Record<string, unknown>);
  return cmRequestWithMeta<TaskListItem[]>(`/tg/tasks${qs ? `?${qs}` : ""}`);
}

/** GET /tg/tasks/{key}/graph — 获取 Task 完整执行链图 */
async function getTaskGraph(key: string): Promise<GraphData> {
  return cmRequest<GraphData>(`/tg/tasks/${encodeKey(key)}/graph`);
}

/** PATCH /tg/tasks/{key}/status — 更新 Task 状态 */
async function updateTaskStatus(
  key: string,
  payload: TaskStatusUpdatePayload,
): Promise<void> {
  await cmRequest<void>(`/tg/tasks/${encodeKey(key)}/status`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** POST /tg/tasks/batch-archive — 批量归档 Tasks */
async function batchArchiveTasks(
  payload: TaskBatchArchivePayload,
): Promise<void> {
  await cmRequest<void>("/tg/tasks/batch-archive", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** DELETE /tg/tasks/{key} — 删除 Task（硬删除，级联删除子节点） */
async function deleteTask(key: string): Promise<void> {
  await cmRequest<void>(`/tg/tasks/${encodeKey(key)}`, {
    method: "DELETE",
  });
}

/** POST /tg/tasks/batch-delete — 批量删除 Tasks */
async function batchDeleteTasks(
  payload: TaskBatchDeletePayload,
): Promise<void> {
  await cmRequest<void>("/tg/tasks/batch-delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

// ------------------------------------------------------------
// Claim Management
// ------------------------------------------------------------

/** GET /tg/claims — 全局 Claim 列表（支持 subject_type, valid, search 过滤 + 分页） */
async function listClaims(
  params: ClaimListParams = {},
): Promise<{ data: ClaimListItem[]; meta: PaginationMeta | null }> {
  const qs = buildPageQuery(params as Record<string, unknown>);
  return cmRequestWithMeta<ClaimListItem[]>(`/tg/claims${qs ? `?${qs}` : ""}`);
}

/** GET /tg/tasks/{taskKey}/claims — 按 Task 查看 Claims */
async function listClaimsByTask(
  taskKey: string,
  params: { page?: number; page_size?: number } = {},
): Promise<{ data: ClaimListItem[]; meta: PaginationMeta | null }> {
  const qs = buildPageQuery(params as Record<string, unknown>);
  return cmRequestWithMeta<ClaimListItem[]>(
    `/tg/tasks/${encodeKey(taskKey)}/claims${qs ? `?${qs}` : ""}`,
  );
}

/** PATCH /tg/claims/{key} — 更新 Claim 字段 */
async function updateClaim(
  key: string,
  payload: ClaimUpdatePayload,
): Promise<{ key: string }> {
  return cmRequest<{ key: string }>(`/tg/claims/${encodeKey(key)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** POST /tg/claims/{key}/invalidate — 标记 Claim 无效 */
async function invalidateClaim(
  key: string,
  payload: ClaimInvalidatePayload,
): Promise<void> {
  await cmRequest<void>(`/tg/claims/${encodeKey(key)}/invalidate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

// ------------------------------------------------------------
// Strategy Card Management
// ------------------------------------------------------------

/** GET /tg/strategies — Strategy Card 列表（支持 polarity, memory_tier 过滤 + 分页） */
async function listStrategies(
  params: StrategyListParams = {},
): Promise<{ data: StrategyCard[]; meta: PaginationMeta | null }> {
  const qs = buildPageQuery(params as Record<string, unknown>);
  return cmRequestWithMeta<StrategyCard[]>(`/tg/strategies${qs ? `?${qs}` : ""}`);
}

/** GET /tg/strategies/{key} — 获取 Strategy Card 详情 */
async function getStrategy(key: string): Promise<StrategyCard> {
  return cmRequest<StrategyCard>(`/tg/strategies/${encodeKey(key)}`);
}

/** PATCH /tg/strategies/{key} — 更新 Strategy Card */
async function updateStrategy(
  key: string,
  payload: StrategyUpdatePayload,
): Promise<void> {
  await cmRequest<void>(`/tg/strategies/${encodeKey(key)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** POST /tg/strategies/{key}/invalidate — 废弃 Strategy Card */
async function invalidateStrategy(
  key: string,
  payload: StrategyInvalidatePayload,
): Promise<void> {
  await cmRequest<void>(`/tg/strategies/${encodeKey(key)}/invalidate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

// ------------------------------------------------------------
// Tag Management
// ------------------------------------------------------------

/** GET /tg/tags — Tag 列表（支持 category, search 过滤 + 分页） */
async function listTags(
  params: TagListParams = {},
): Promise<{ data: TagListItem[]; meta: PaginationMeta | null }> {
  const qs = buildPageQuery(params as Record<string, unknown>);
  return cmRequestWithMeta<TagListItem[]>(`/tg/tags${qs ? `?${qs}` : ""}`);
}

// ------------------------------------------------------------
// Exported API object
// ------------------------------------------------------------

export const tgApi = {
  // Task
  listTasks,
  getTaskGraph,
  updateTaskStatus,
  batchArchiveTasks,
  deleteTask,
  batchDeleteTasks,
  // Claim
  listClaims,
  listClaimsByTask,
  updateClaim,
  invalidateClaim,
  // Strategy Card
  listStrategies,
  getStrategy,
  updateStrategy,
  invalidateStrategy,
  // Tag
  listTags,
};
