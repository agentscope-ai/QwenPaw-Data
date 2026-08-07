import React, { useState } from 'react';
import { Upload, Card, message,  Space, Typography, Result, Button } from '@/design';
import { InboxOutlined } from '@/design';
import { importExcel, type ExcelImportResult } from '@/services/excelImport';
import { useTranslation } from 'react-i18next';

const { Dragger } = Upload;
const { Text } = Typography;

const ExcelImportPage: React.FC = () => {
  const { t } = useTranslation();
  const [uploading, setUploading] = useState(false);
  const [result, setResult] = useState<ExcelImportResult | null>(null);

  const handleUpload = async (file: File) => {
    // 校验文件格式
    const validTypes = [
      'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      'application/vnd.ms-excel',
    ];
    const isXlsx = file.name.endsWith('.xlsx') || file.name.endsWith('.xls');
    if (!isXlsx && !validTypes.includes(file.type)) {
      message.error(t('excelImport.onlyExcel'));
      return false;
    }
    // 校验文件大小（50MB）
    const maxSize = 50 * 1024 * 1024;
    if (file.size > maxSize) {
      message.error(t('excelImport.fileTooLarge'));
      return false;
    }

    setUploading(true);
    setResult(null);

    try {
      const importResult = await importExcel(file);
      setResult(importResult);
      if (importResult.success && importResult.errors.length === 0) {
        message.success(t('excelImport.success'));
      } else if (importResult.success) {
        message.warning(t('excelImport.partialSuccess'));
      } else {
        message.error(t('excelImport.failed'));
      }
    } catch (error) {
      console.error('Excel导入异常:', error);
      // API 错误由响应拦截器统一提示
    } finally {
      setUploading(false);
    }

    return false; // 阻止默认上传行为
  };

  const isSuccess = !!result?.success && result.errors.length === 0;

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      {isSuccess ? (
        /* 上传成功：隐藏上传组件，仅展示成功结果 */
        <Card>
          <Result
            status="success"
            title={t('excelImport.successTitle')}
            subTitle={t('excelImport.successSubtitle')}
            extra={[
              <Button type="primary" key="reupload" onClick={() => setResult(null)}>
                {t('excelImport.continueUpload')}
              </Button>,
            ]}
          />
        </Card>
      ) : (
        /* 上传区域 */
        <Card>
          <Dragger
            accept=".xlsx"
            maxCount={1}
            beforeUpload={handleUpload}
            showUploadList={false}
            disabled={uploading}
          >
            <p className="ant-upload-drag-icon">
              <InboxOutlined />
            </p>
            <p className="ant-upload-text">{t('excelImport.uploadText')}</p>
            <p className="ant-upload-hint">
              {t('excelImport.uploadHint')}
            </p>
          </Dragger>
          {uploading && (
            <div style={{ textAlign: 'center', marginTop: 16 }}>
              <Text type="secondary">{t('excelImport.uploading')}</Text>
            </div>
          )}
        </Card>
      )}
    </Space>
  );
};

export default ExcelImportPage;
