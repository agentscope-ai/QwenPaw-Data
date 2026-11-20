import { semanticConfigApi } from './api';
import { request } from '@/utils/request';
import type {
  ProfilingOverviewParams,
  ProfilingOverviewRecord,
  ProfilingBatchActionResponse,
  ProfilingCandidateStagePath,
  ProfilingPage,
  ProfilingResultRow,
  ProfilingStagePath,
  ProfilingStageActionResponse,
  ProfilingStatusResponse,
  ProfilingTask,
} from '@/types/dataProfiling';

const probeApi = (path: string) => semanticConfigApi(`/probe${path}`);
const stageApi = (datasourceId: string, stage: ProfilingStagePath, path = '') =>
  probeApi(`/${encodeURIComponent(datasourceId)}/${stage}${path}`);

export const queryProfilingOverview = (params: ProfilingOverviewParams) =>
  request.get<ProfilingPage<ProfilingOverviewRecord>>(probeApi('/overview'), { params });

export const getProfilingStatus = (datasourceId: string) =>
  request.get<ProfilingStatusResponse>(probeApi(`/${encodeURIComponent(datasourceId)}/status`));

export const startProfilingStage = (datasourceId: string, stage: ProfilingStagePath) =>
  request.post<ProfilingTask>(stageApi(datasourceId, stage, '/start'));

export const refineProfilingStage = (datasourceId: string, stage: ProfilingStagePath) =>
  request.post<ProfilingTask>(stageApi(datasourceId, stage, '/refine'));

export const rerunProfilingStage = (datasourceId: string, stage: ProfilingStagePath) =>
  request.post<ProfilingTask>(stageApi(datasourceId, stage, '/rerun'));

export const stopProfilingStage = (datasourceId: string, stage: ProfilingStagePath) =>
  request.post<ProfilingStageActionResponse>(stageApi(datasourceId, stage, '/stop'));

export const skipProfilingStage = (datasourceId: string, stage: ProfilingStagePath) =>
  request.post<ProfilingStageActionResponse>(stageApi(datasourceId, stage, '/skip'));

export const completeProfilingStage = (datasourceId: string, stage: ProfilingStagePath) =>
  request.post<ProfilingStageActionResponse>(stageApi(datasourceId, stage, '/complete'));

export const getProfilingTask = (datasourceId: string, stage: ProfilingStagePath) =>
  request.get<ProfilingTask>(stageApi(datasourceId, stage, '/task'));

export const queryProfilingResults = (
  datasourceId: string,
  stage: ProfilingStagePath,
  params: Record<string, unknown>,
) =>
  request.get<ProfilingPage<ProfilingResultRow>>(stageApi(datasourceId, stage, '/results'), {
    params,
  });

export const addProfilingRow = (
  datasourceId: string,
  stage: ProfilingCandidateStagePath,
  data: Record<string, unknown>,
) => request.post<ProfilingResultRow>(stageApi(datasourceId, stage, '/row'), data);

export const updateProfilingRow = (
  datasourceId: string,
  stage: ProfilingStagePath,
  rowId: number,
  data: Record<string, unknown>,
) => request.put<ProfilingResultRow>(stageApi(datasourceId, stage, `/row/${rowId}`), data);

export const deleteProfilingRow = (
  datasourceId: string,
  stage: ProfilingCandidateStagePath,
  rowId: number,
) => request.delete(stageApi(datasourceId, stage, `/row/${rowId}`));

export const batchConfirmProfilingRows = (
  datasourceId: string,
  stage: ProfilingCandidateStagePath,
  rowIds: number[],
) =>
  request.post<ProfilingBatchActionResponse>(stageApi(datasourceId, stage, '/row/confirm'), {
    row_ids: rowIds,
  });

export const batchSkipProfilingRows = (
  datasourceId: string,
  stage: ProfilingCandidateStagePath,
  rowIds: number[],
) =>
  request.post<ProfilingBatchActionResponse>(stageApi(datasourceId, stage, '/row/skip'), {
    row_ids: rowIds,
  });

export const batchDeleteProfilingRows = (
  datasourceId: string,
  stage: ProfilingCandidateStagePath,
  rowIds: number[],
) =>
  request.post<ProfilingBatchActionResponse>(stageApi(datasourceId, stage, '/row/delete'), {
    row_ids: rowIds,
  });

function fileBody(file: File): FormData {
  const body = new FormData();
  body.append('file', file);
  return body;
}

export const uploadProfilingStage = (datasourceId: string, stage: ProfilingStagePath, file: File) =>
  request.post(stageApi(datasourceId, stage, '/upload'), fileBody(file));

export const exportProfilingStage = (datasourceId: string, stage: ProfilingStagePath) =>
  request.get<Blob>(stageApi(datasourceId, stage, '/export'), { responseType: 'blob' });
