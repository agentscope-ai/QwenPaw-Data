import type { ConfirmWeavingParams, SemanticWeavingTaskQueryParams } from '@/types/semanticWeaving';
import { request } from '@/utils/request';
import { semanticConfigApi } from './api';

const WEAVE_TASK_API = semanticConfigApi('/weave-task');

/**
 * 发起编织任务
 */
export const confirmSemanticWeaving = (
  params: ConfirmWeavingParams
) => {
  return request.post(`${WEAVE_TASK_API}/submit`, params);
};

/**
 * 查询编织任务列表
 */
export const querySemanticWeavingTasks = (params?: SemanticWeavingTaskQueryParams) => {
  return request.get(WEAVE_TASK_API, { params });
};

/**
 * 杀死编织任务
 */
export const killSemanticWeavingTask = (taskId: string) => {
  return request.post(`${WEAVE_TASK_API}/${taskId}/kill`);
};
