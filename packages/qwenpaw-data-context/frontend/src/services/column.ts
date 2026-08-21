import { QueryColumnParams, ColumnItem } from "@/types/column"
import { request } from "@/utils/request"
import { semanticConfigApi } from "./api"

const COLUMN_API = semanticConfigApi('/dataset-column-meta')

// 查询列列表
export const queryColumnList = (params: QueryColumnParams) => {
  return request.get(COLUMN_API, { params })
}

// 创建列（单个）
export const createColumn = (data: Partial<ColumnItem>) => {
  return request.post(COLUMN_API, data)
}

// 更新列
export const updateColumn = (id: number, data: Partial<ColumnItem>) => {
  return request.put(`${COLUMN_API}/${id}`, data)
}

// 删除列
export const deleteColumn = (id: number) => {
  return request.delete(`${COLUMN_API}/${id}`)
}

// Step 1 - 预览
export const previewColumnMeta = (datasetId: number) => {
  return request.get(`${COLUMN_API}/dataset/${datasetId}`)
}

const persistColumns = (datasetId: number, data: ColumnItem[]) => {
  return Promise.all(
    data.map((item) => {
      const payload = { ...item, dataset_id: item.dataset_id ?? datasetId };
      return typeof item.id === 'number'
        ? updateColumn(item.id, payload)
        : createColumn(payload);
    })
  );
};

// Step 2 - 确认入库
export const confirmColumnMetaStorage = (datasetId: number, data: ColumnItem[]) => {
  return persistColumns(datasetId, data)
}

// Step 3 - 维度推理
export const inferDimensions = (datasetId: number, data: ColumnItem[]) => {
  void datasetId;
  return Promise.resolve(data)
}

// Step 4 - 确认维度
export const confirmDimensions = (datasetId: number, data: ColumnItem[]) => {
  return persistColumns(datasetId, data)
}

// Step 5 - 样本补全
export const completeSamples = (datasetId: number) => {
  return previewColumnMeta(datasetId)
}

// Step 6 - 确认样本
export const confirmSamples = (datasetId: number, data: ColumnItem[]) => {
  return persistColumns(datasetId, data)
}
