import { QueryMetricFormulaLibParams, MetricFormulaLibItem } from "@/types/metricFormulaLib"
import { request } from "@/utils/request"
import { semanticConfigApi } from "./api"

const METRIC_FORMULA_API = semanticConfigApi('/metric-formula-lib')

export const createMetricFormula = (data: Partial<MetricFormulaLibItem>) => {
  return request.post(METRIC_FORMULA_API, data)
}

export const createMetricFormulaBatch = (data: Partial<MetricFormulaLibItem>[]) => {
  return Promise.all(data.map((item) => createMetricFormula(item)))
}

export const queryMetricFormulaList = (params: QueryMetricFormulaLibParams) => {
  return request.get(METRIC_FORMULA_API, { params })
}

export const updateMetricFormula = (id: number, data: Partial<MetricFormulaLibItem>) => {
  return request.put(`${METRIC_FORMULA_API}/${id}`, data)
}

export const deleteMetricFormula = (id: number) => {
  return request.delete(`${METRIC_FORMULA_API}/${id}`)
}
