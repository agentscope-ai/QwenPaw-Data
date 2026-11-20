import { request } from '@/utils/request';
import { semanticConfigApi } from './api';
import { requestBlobDownload } from '@/services/request';

export const SEMANTIC_EXCEL_MAX_SIZE_BYTES = 20 * 1024 * 1024;

export const SEMANTIC_EXCEL_TEMPLATE_OBJECTS = [
  'biz_domain',
  'dataset',
  'dataset_column',
  'dimension',
  'dataset_dimension',
  'metric',
  'metric_formula',
] as const;

export type SemanticExcelObject = (typeof SEMANTIC_EXCEL_TEMPLATE_OBJECTS)[number];
export type ExcelTemplateObject = 'all' | SemanticExcelObject;

export type SemanticExcelFileValidationError = 'extension' | 'size';

export interface ExcelDownload {
  blob: Blob;
  filename: string;
}

export interface ExcelDownloadAnchor {
  href: string;
  download: string;
  click: () => void;
  remove: () => void;
}

export interface ExcelDownloadEnvironment {
  createObjectURL: (blob: Blob) => string;
  revokeObjectURL: (url: string) => void;
  createAnchor: () => ExcelDownloadAnchor;
  appendAnchor: (anchor: ExcelDownloadAnchor) => void;
}

export interface ExcelImportSummary {
  biz_domain: number;
  dataset: number;
  dataset_column: number;
  dimension: number;
  dataset_dimension: number;
  metric: number;
  metric_formula: number;
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

export function validateSemanticExcelFile(
  file: Pick<File, 'name' | 'size'>,
): SemanticExcelFileValidationError | undefined {
  if (!file.name.toLowerCase().endsWith('.xlsx')) return 'extension';
  if (file.size > SEMANTIC_EXCEL_MAX_SIZE_BYTES) return 'size';
  return undefined;
}

export function isSuccessfulExcelImport(result: ExcelImportResult): boolean {
  return result.success && result.errors.length === 0;
}

function safeFilenamePart(value: string): string {
  return (
    value
      .trim()
      .replace(/[^a-zA-Z0-9._-]+/g, '-')
      .replace(/^-+|-+$/g, '') || 'semantic-config'
  );
}

function removeControlCharacters(value: string): string {
  return [...value]
    .filter((character) => {
      const code = character.charCodeAt(0);
      return code > 31 && code !== 127;
    })
    .join('');
}

function normalizeExcelFilename(filename: string | undefined, fallback: string): string {
  const basename = filename?.split(/[\\/]/).pop()?.trim();
  const sanitizedBasename = basename ? removeControlCharacters(basename) : undefined;
  const resolved = sanitizedBasename || fallback;
  return resolved.toLowerCase().endsWith('.xlsx') ? resolved : `${resolved}.xlsx`;
}

function browserDownloadEnvironment(): ExcelDownloadEnvironment {
  return {
    createObjectURL: (blob) => URL.createObjectURL(blob),
    revokeObjectURL: (url) => URL.revokeObjectURL(url),
    createAnchor: () => document.createElement('a'),
    appendAnchor: (anchor) => document.body.appendChild(anchor as HTMLAnchorElement),
  };
}

export function saveExcelDownload(
  download: ExcelDownload,
  environment: ExcelDownloadEnvironment = browserDownloadEnvironment(),
): void {
  const objectUrl = environment.createObjectURL(download.blob);
  const anchor = environment.createAnchor();
  anchor.href = objectUrl;
  anchor.download = download.filename;
  environment.appendAnchor(anchor);
  try {
    anchor.click();
  } finally {
    anchor.remove();
    environment.revokeObjectURL(objectUrl);
  }
}

export const importExcel = (file: File, datasourceId: string): Promise<ExcelImportResult> => {
  const formData = new FormData();
  formData.append('file', file);
  formData.append('datasource_id', datasourceId);
  return request.post(semanticConfigApi('/import/excel'), formData, {
    headers: { 'Content-Type': undefined },
  });
};

export const exportSemanticConfigExcel = async (datasourceId: string): Promise<ExcelDownload> => {
  const query = new URLSearchParams({ datasource_id: datasourceId });
  const response = await requestBlobDownload(
    `${semanticConfigApi('/export/excel')}?${query.toString()}`,
  );
  return {
    blob: response.blob,
    filename: normalizeExcelFilename(
      response.filename,
      `semantic-config-${safeFilenamePart(datasourceId)}.xlsx`,
    ),
  };
};

export const downloadExcelTemplate = async (
  object: ExcelTemplateObject = 'all',
): Promise<ExcelDownload> => {
  const query = new URLSearchParams({ object });
  const response = await requestBlobDownload(
    `${semanticConfigApi('/import/excel/template')}?${query.toString()}`,
  );
  return {
    blob: response.blob,
    filename: normalizeExcelFilename(response.filename, `semantic-config-template-${object}.xlsx`),
  };
};
