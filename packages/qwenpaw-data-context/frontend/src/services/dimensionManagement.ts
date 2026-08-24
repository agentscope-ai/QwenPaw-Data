import { QueryDimensionParams, DimensionItem, QueryDimensionCaliberParams, DimensionCaliberItem } from "@/types/dimensionManagement"
import { request } from "@/utils/request"
import { semanticConfigApi } from "./api"

const DIMENSION_API = semanticConfigApi('/dimension')
const DIMENSION_CALIBER_API = semanticConfigApi('/dataset-dimension')

// ==================== 维度主表 /api/semantic-config/dimension ====================

export const queryDimensionList = (params: QueryDimensionParams) => {
  return request.get(DIMENSION_API, { params })
}

export const createDimension = (data: Partial<DimensionItem>) => {
  return request.post(DIMENSION_API, data)
}

export const updateDimension = (id: number, data: Partial<DimensionItem>) => {
  return request.put(`${DIMENSION_API}/${id}`, data)
}

export const deleteDimension = (id: number) => {
  return request.delete(`${DIMENSION_API}/${id}`)
}

// ==================== 维度口径 /api/semantic-config/dataset-dimension ====================

export const previewDimension = (datasetIds: number[]) => {
  return Promise.all(
    datasetIds.map((datasetId) => request.get<DimensionCaliberItem[]>(`${DIMENSION_CALIBER_API}/dataset/${datasetId}`))
  ).then((results) => results.flat())
}

export const confirmDimensionStorage = (data: DimensionCaliberItem[]) => {
  return Promise.all(
    data.map((item) =>
      typeof item.id === 'number'
        ? updateDimensionCaliber(item.id, item)
        : createDimensionCaliber(item)
    )
  )
}

export const queryDimensionCaliberList = (params: QueryDimensionCaliberParams) => {
  return request.get(DIMENSION_CALIBER_API, { params })
}

export const createDimensionCaliber = (data: Partial<DimensionCaliberItem>) => {
  return request.post(DIMENSION_CALIBER_API, data)
}

export const updateDimensionCaliber = (id: number, data: Partial<DimensionCaliberItem>) => {
  return request.put(`${DIMENSION_CALIBER_API}/${id}`, data)
}

export const deleteDimensionCaliber = (id: number) => {
  return request.delete(`${DIMENSION_CALIBER_API}/${id}`)
}
