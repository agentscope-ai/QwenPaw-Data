import { QueryDatasetMetaParams, DatasetManagementItem } from "@/types/datasetManagement"
import { request } from "@/utils/request"
import { semanticConfigApi } from "./api"

export const queryDatasetMeta = (params: QueryDatasetMetaParams) => {
  return request.get(semanticConfigApi('/dataset-meta'), { params })
}

export const createDatasetMeta = (data: Partial<DatasetManagementItem>) => {
  return request.post(semanticConfigApi('/dataset-meta'), data)
}

export const updateDatasetMeta = (id: number, data: Partial<DatasetManagementItem>) => {
  return request.put(semanticConfigApi(`/dataset-meta/${id}`), data)
}

export const deleteDatasetMeta = (id: number) => {
  return request.delete(semanticConfigApi(`/dataset-meta/${id}`))
}
