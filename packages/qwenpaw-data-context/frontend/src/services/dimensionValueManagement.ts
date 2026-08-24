import { QueryDimensionValueParams, DimensionValueItem } from "@/types/dimensionValueManagement"
import { message } from "@/design"

const unsupportedDimensionValueApi = <T = unknown>(): Promise<T> => {
  const error = new Error('当前 qwenpaw-data-context 后端暂未暴露维度值管理接口');
  message.error(error.message);
  return Promise.reject(error);
}

// 预览（不写库）
export const previewDimensionValue = (datasetIds: number[]) => {
  void datasetIds;
  return unsupportedDimensionValueApi<DimensionValueItem[]>()
}

// 确认入库
export const confirmDimensionValueStorage = (data: DimensionValueItem[]) => {
  void data;
  return unsupportedDimensionValueApi()
}

// 列表（分页）
export const queryDimensionValueList = (params: QueryDimensionValueParams) => {
  void params;
  return unsupportedDimensionValueApi()
}

// 删除
export const deleteDimensionValue = (id: number) => {
  void id;
  return unsupportedDimensionValueApi()
}
