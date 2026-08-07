import { request } from '@/utils/request';
import { semanticConfigApi } from './api';

export interface ExcelImportSummary {
  datasource?: number;
  biz_domain?: number;
  dataset?: number;
  dataset_column?: number;
  dimension?: number;
  dataset_dimension?: number;
  metric?: number;
  metric_formula?: number;
}

export interface ExcelImportError {
  sheet: string;
  row: number;
  message: string;
}

export interface ExcelImportResult {
  success: boolean;
  summary: ExcelImportSummary;
  errors: ExcelImportError[];
}

/**
 * 上传 Excel 文件并同步导入
 */
export const importExcel = (file: File): Promise<ExcelImportResult> => {
  const formData = new FormData();
  formData.append('file', file);
  // 置为 undefined 以清除实例默认的 application/json，
  // 否则 axios 会把 FormData 序列化为 JSON；清除后 axios 会自动生成带 boundary 的 multipart 头
  return request.post(semanticConfigApi('/import/excel'), formData, {
    headers: { 'Content-Type': undefined },
  });
};
