import * as XLSX from 'xlsx';
import { message } from '@/design';
import { generateMetricId, type MetricItem } from '../useMetricAdd';
import i18n from '@/i18n';

// 重新导出类型，便于其他文件使用
export type { MetricItem } from '../useMetricAdd';

// Excel 模板列配置
const TEMPLATE_COLUMNS = [
  { headerKey: 'fields.metricName', key: 'metricName', required: true },
  { headerKey: 'metricAdd.domain', key: 'domain', required: false },
  { headerKey: 'fields.synonyms', key: 'synonyms', required: false },
];

// 生成唯一ID
const generateId = generateMetricId;

/**
 * 下载 Excel 模板
 * 生成包含表头和示例数据的 .xlsx 文件
 */
export const downloadExcelTemplate = () => {
  // 创建工作簿
  const workbook = XLSX.utils.book_new();

  // 表头
  const headers = TEMPLATE_COLUMNS.map((col) => i18n.t(col.headerKey));

  // 示例数据（帮助用户理解格式）
  const sampleData: string[][] = [];

  // 组合数据
  const data = [headers, ...sampleData];

  // 创建工作表
  const worksheet = XLSX.utils.aoa_to_sheet(data);

  // 设置列宽
  worksheet['!cols'] = [
    { wch: 20 }, // 指标名称
    { wch: 15 }, // 所属域
    { wch: 30 }, // 同义词
  ];

  // 添加工作表到工作簿
  XLSX.utils.book_append_sheet(workbook, worksheet, i18n.t('metricAdd.excel.templateSheet'));

  // 导出文件
  XLSX.writeFile(workbook, `${i18n.t('metricAdd.excel.templateFile')}.xlsx`);

  message.success(i18n.t('metricAdd.excel.templateDownloadSuccess'));
};

/**
 * 解析 Excel/CSV 文件
 * @param file 上传的文件
 * @returns Promise<MetricItem[]> 解析后的指标数据
 */
export const parseExcelFile = (file: File): Promise<MetricItem[]> => {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();

    reader.onload = (e) => {
      try {
        const data = e.target?.result;
        const workbook = XLSX.read(data, { type: 'binary' });

        // 获取第一个工作表
        const sheetName = workbook.SheetNames[0];
        const worksheet = workbook.Sheets[sheetName];

        // 将工作表转换为 JSON
        const jsonData = XLSX.utils.sheet_to_json<string[]>(worksheet, {
          header: 1, // 使用数组格式，第一行作为数据
          defval: '', // 默认空值
        });

        if (jsonData.length < 2) {
          message.warning(i18n.t('metricAdd.excel.noValidRowsInFile'));
          resolve([]);
          return;
        }

        // 获取表头行（第一行）
        const headerRow = jsonData[0];

        // 查找列索引
        const metricNameIndex = findColumnIndex(headerRow, ['指标名称', 'metricName', 'Metric Name']);
        const domainIndex = findColumnIndex(headerRow, ['所属域', 'domain', 'Domain']);
        const synonymsIndex = findColumnIndex(headerRow, ['同义词', 'synonyms', 'Synonyms']);

        if (metricNameIndex === -1) {
          message.error(i18n.t('metricAdd.excel.metricNameColumnMissing'));
          resolve([]);
          return;
        }

        // 解析数据行（跳过表头）
        const parsedData: MetricItem[] = [];
        let invalidCount = 0;

        for (let i = 1; i < jsonData.length; i++) {
          const row = jsonData[i];
          const metricName = (row[metricNameIndex] || '').toString().trim();
          const domain = domainIndex !== -1 ? (row[domainIndex] || '').toString().trim() : '';
          const synonyms = synonymsIndex !== -1 ? (row[synonymsIndex] || '').toString().trim() : '';

          // 跳过完全空的行
          if (!metricName && !domain && !synonyms) {
            continue;
          }

          // 验证必填字段
          if (!metricName) {
            invalidCount++;
            continue;
          }

          parsedData.push({
            id: generateId(),
            metricName,
            domain,
            synonyms,
          });
        }

        if (invalidCount > 0) {
          message.warning(i18n.t('metricAdd.excel.skippedMissingMetricName', { count: invalidCount }));
        }

        if (parsedData.length === 0) {
          message.warning(i18n.t('metricAdd.excel.noValidImportedData'));
        } else {
          message.success(i18n.t('metricAdd.excel.importSuccess', { count: parsedData.length }));
        }

        resolve(parsedData);
      } catch (error) {
        message.error(i18n.t('metricAdd.excel.parseFailed'));
        reject(error);
      }
    };

    reader.onerror = () => {
      const errorMessage = i18n.t('metricAdd.excel.readFailed');
      message.error(errorMessage);
      reject(new Error(errorMessage));
    };

    // 读取文件
    reader.readAsBinaryString(file);
  });
};

/**
 * 查找列索引（支持多个可能的列名）
 */
const findColumnIndex = (headerRow: string[], possibleNames: string[]): number => {
  for (let i = 0; i < headerRow.length; i++) {
    const header = (headerRow[i] || '').toString().trim().toLowerCase();
    for (const name of possibleNames) {
      if (header === name.toLowerCase()) {
        return i;
      }
    }
  }
  return -1;
};

/**
 * 导出当前数据为 Excel
 * @param data 要导出的数据
 * @param filename 文件名
 */
export const exportDataToExcel = (data: MetricItem[], filename: string = i18n.t('metricAdd.excel.dataFile')) => {
  if (data.length === 0) {
    message.warning(i18n.t('metricAdd.excel.noDataToExport'));
    return;
  }

  // 创建工作簿
  const workbook = XLSX.utils.book_new();

  // 转换数据格式
  const exportData = data.map((item: MetricItem) => ({
    [i18n.t('fields.metricName')]: item.metricName,
    [i18n.t('metricAdd.domain')]: item.domain,
    [i18n.t('fields.synonyms')]: item.synonyms,
  }));

  // 创建工作表
  const worksheet = XLSX.utils.json_to_sheet(exportData);

  // 设置列宽
  worksheet['!cols'] = [
    { wch: 20 },
    { wch: 15 },
    { wch: 30 },
  ];

  // 添加工作表到工作簿
  XLSX.utils.book_append_sheet(workbook, worksheet, i18n.t('metricAdd.excel.dataSheet'));

  // 导出文件
  XLSX.writeFile(workbook, `${filename}.xlsx`);

  message.success(i18n.t('metricAdd.excel.exportSuccess'));
};
