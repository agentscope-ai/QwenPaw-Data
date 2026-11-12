import type {
  ConfirmWeavingParams,
  SemanticWeavingTask,
  SemanticWeavingTaskPage,
  SemanticWeavingTaskQueryParams,
} from '@/types/semanticWeaving';
import { assertSemanticConfigPage } from '@/utils/semanticConfigContract';
import { request } from '@/utils/request';
import { semanticConfigApi } from './api';

const WEAVE_TASK_API = semanticConfigApi('/weave-task');

export const confirmSemanticWeaving = (
  params: ConfirmWeavingParams,
): Promise<SemanticWeavingTask> => {
  return request.post(`${WEAVE_TASK_API}/submit`, params);
};

export const querySemanticWeavingTasks = async (
  params: SemanticWeavingTaskQueryParams = {},
): Promise<SemanticWeavingTaskPage> => {
  const response = await request.get<unknown>(WEAVE_TASK_API, {
    params: { ...params, page: params.page ?? 1, size: params.size ?? 20 },
  });
  return assertSemanticConfigPage<SemanticWeavingTask>(response);
};

export const killSemanticWeavingTask = (taskId: string): Promise<SemanticWeavingTask> => {
  return request.post(`${WEAVE_TASK_API}/${taskId}/kill`);
};
