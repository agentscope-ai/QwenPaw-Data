import { QueryMetricLibParams, MetricLibItem } from "@/types/metricLib"
import { request } from "@/utils/request"
import { semanticConfigApi } from "./api"

const METRIC_API = semanticConfigApi('/metric-lib')

export const createMetric = (data: Partial<MetricLibItem>) => {
  return request.post(METRIC_API, data)
}

export const confirmCreateMetric = (data: MetricLibItem[]) => {
  return Promise.all(data.map((item) => createMetric(item)))
}

export const checkDuplicates = (data: MetricLibItem[]) => {
  void data;
  return Promise.resolve([])
}

export const queryMetricList = (params: QueryMetricLibParams) => {
  return request.get(METRIC_API, { params })
}

export const updateMetric = (id: number, data: Partial<MetricLibItem>) => {
  return request.put(`${METRIC_API}/${id}`, data)
}

export const deleteMetric = (id: number) => {
  return request.delete(`${METRIC_API}/${id}`)
}
