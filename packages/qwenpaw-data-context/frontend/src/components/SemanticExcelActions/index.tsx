import {
  Button,
  DownloadOutlined,
  FileExcelOutlined,
  MoreOutlined,
  Modal,
  Select,
  Space,
  Typography,
  UploadOutlined,
  message,
} from '@/design';
import { Dropdown, Upload as AntUpload } from 'antd';
import type { MenuProps, UploadProps } from 'antd';
import {
  downloadExcelTemplate,
  exportSemanticConfigExcel,
  importExcel,
  isSuccessfulExcelImport,
  saveExcelDownload,
  validateSemanticExcelFile,
  type ExcelImportResult,
  type ExcelTemplateObject,
} from '@/services/excelImport';
import React, { useState } from 'react';
import { useDataSourceOptions, useDataSourceOptionsStore } from '@/store/dataSourceOptions';
import { useTranslation } from 'react-i18next';

interface SemanticExcelActionsProps {
  datasourceId?: string;
  getDatasourceId?: () => string | undefined;
  templateObject: ExcelTemplateObject;
  showExport?: boolean;
  buttonType?: 'default' | 'link';
  displayMode?: 'buttons' | 'dropdown';
  dropdownLabel?: React.ReactNode;
  dropdownTrigger?: 'click' | 'hover';
  onImportSuccess?: () => void;
}

type LoadingAction = 'template' | 'import' | 'export' | null;

function ImportFailureDetails({ result }: { result: ExcelImportResult }) {
  const { t } = useTranslation();
  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Typography.Text type="danger">{t('excelImport.rollbackNotice')}</Typography.Text>
      <div style={{ maxHeight: 320, overflow: 'auto' }}>
        {result.errors.map((error, index) => (
          <div key={`${error.sheet}-${error.row}-${index}`}>
            <Typography.Text>
              {error.row > 0
                ? t('excelImport.errorLine', {
                    sheet: error.sheet,
                    row: error.row,
                    message: error.message,
                  })
                : t('excelImport.errorMessage', {
                    sheet: error.sheet,
                    message: error.message,
                  })}
            </Typography.Text>
          </div>
        ))}
      </div>
    </Space>
  );
}

const SemanticExcelActions: React.FC<SemanticExcelActionsProps> = ({
  datasourceId,
  getDatasourceId,
  templateObject,
  showExport = false,
  buttonType = 'default',
  displayMode = 'buttons',
  dropdownLabel,
  dropdownTrigger = 'click',
  onImportSuccess,
}) => {
  const { t } = useTranslation();
  const [loadingAction, setLoadingAction] = useState<LoadingAction>(null);
  const [importModalOpen, setImportModalOpen] = useState(false);
  const [selectedDatasourceId, setSelectedDatasourceId] = useState<string>();
  const [exportModalOpen, setExportModalOpen] = useState(false);
  const dataSourceOptions = useDataSourceOptions();

  const resolveDatasourceId = () =>
    (datasourceId || selectedDatasourceId || getDatasourceId?.() || '').trim();

  const requireDatasourceId = (): string | undefined => {
    const currentDatasourceId = resolveDatasourceId();
    if (!currentDatasourceId) {
      message.warning(t('excelImport.selectDatasource'));
      return undefined;
    }
    return currentDatasourceId;
  };

  const handleTemplateDownload = async () => {
    setLoadingAction('template');
    try {
      saveExcelDownload(await downloadExcelTemplate(templateObject));
      message.success(t('excelImport.templateSuccess'));
    } catch {
      message.error(t('excelImport.downloadFailed'));
    } finally {
      setLoadingAction(null);
    }
  };

  const handleExport = async () => {
    const currentDatasourceId = resolveDatasourceId();
    if (!currentDatasourceId) {
      void useDataSourceOptionsStore.getState().fetchOptions(true);
      setExportModalOpen(true);
      return;
    }
    setLoadingAction('export');
    try {
      saveExcelDownload(await exportSemanticConfigExcel(currentDatasourceId));
      message.success(t('excelImport.exportSuccess'));
      setExportModalOpen(false);
    } catch {
      message.error(t('excelImport.downloadFailed'));
    } finally {
      setLoadingAction(null);
    }
  };

  const handleImport: NonNullable<UploadProps['beforeUpload']> = async (file) => {
    const currentDatasourceId = requireDatasourceId();
    if (!currentDatasourceId) return AntUpload.LIST_IGNORE;

    const validationError = validateSemanticExcelFile(file);
    if (validationError === 'extension') {
      message.error(t('excelImport.onlyExcel'));
      return AntUpload.LIST_IGNORE;
    }
    if (validationError === 'size') {
      message.error(t('excelImport.fileTooLarge'));
      return AntUpload.LIST_IGNORE;
    }

    setLoadingAction('import');
    try {
      const result = await importExcel(file as File, currentDatasourceId);
      if (isSuccessfulExcelImport(result)) {
        message.success(t('excelImport.success'));
        setImportModalOpen(false);
        onImportSuccess?.();
      } else {
        Modal.error({
          title: t('excelImport.importFailedTitle'),
          content: <ImportFailureDetails result={result} />,
          width: 680,
        });
      }
    } catch {
      // API errors are normalized and displayed by the shared request interceptor.
    } finally {
      setLoadingAction(null);
    }
    return AntUpload.LIST_IGNORE;
  };

  const isBusy = loadingAction !== null;
  const importLabel =
    templateObject === 'all' ? t('excelImport.importModel') : t('excelImport.importObject');
  const handleOpenImportModal = () => {
    setSelectedDatasourceId(getDatasourceId?.() || datasourceId);
    void useDataSourceOptionsStore.getState().fetchOptions(true);
    setImportModalOpen(true);
  };
  const menuItems: MenuProps['items'] = [
    {
      key: 'import',
      icon: <UploadOutlined />,
      label: importLabel,
      disabled: isBusy,
    },
    ...(showExport
      ? [
          {
            key: 'export',
            icon: <FileExcelOutlined />,
            label: t('excelImport.exportModel'),
            disabled: isBusy,
          },
        ]
      : []),
  ];
  const handleMenuClick: MenuProps['onClick'] = ({ key }) => {
    if (key === 'import') handleOpenImportModal();
    if (key === 'export') void handleExport();
  };

  return (
    <>
      {displayMode === 'dropdown' ? (
        <Dropdown menu={{ items: menuItems, onClick: handleMenuClick }} trigger={[dropdownTrigger]}>
          <Button
            type={buttonType}
            icon={dropdownLabel ? undefined : <MoreOutlined />}
            loading={loadingAction === 'export'}
            disabled={isBusy && loadingAction !== 'export'}
          >
            {dropdownLabel ?? t('common.more')}
          </Button>
        </Dropdown>
      ) : (
        <Space size="small">
          <Button
            type={buttonType}
            icon={<DownloadOutlined />}
            loading={loadingAction === 'template'}
            disabled={isBusy && loadingAction !== 'template'}
            onClick={() => void handleTemplateDownload()}
          >
            {t('excelImport.downloadTemplate')}
          </Button>
          <Button
            type={buttonType}
            icon={<UploadOutlined />}
            loading={loadingAction === 'import'}
            disabled={isBusy && loadingAction !== 'import'}
            onClick={handleOpenImportModal}
          >
            {importLabel}
          </Button>
          {showExport ? (
            <Button
              type={buttonType}
              icon={<FileExcelOutlined />}
              loading={loadingAction === 'export'}
              disabled={isBusy && loadingAction !== 'export'}
              onClick={() => void handleExport()}
            >
              {t('excelImport.exportModel')}
            </Button>
          ) : null}
        </Space>
      )}

      <Modal
        title={t('excelImport.exportModel')}
        open={exportModalOpen}
        onCancel={() => setExportModalOpen(false)}
        onOk={() => void handleExport()}
        okButtonProps={{ disabled: !resolveDatasourceId() }}
        confirmLoading={loadingAction === 'export'}
      >
        <Select
          aria-label={t('fields.dataSource')}
          placeholder={t('excelImport.selectDatasource')}
          options={dataSourceOptions}
          value={selectedDatasourceId}
          onChange={setSelectedDatasourceId}
          showSearch
          optionFilterProp="label"
          style={{ width: '100%' }}
        />
      </Modal>
      <Modal
        title={t('excelImport.importTitle')}
        open={importModalOpen}
        footer={null}
        width={640}
        destroyOnHidden
        maskClosable={!isBusy}
        closable={!isBusy}
        onCancel={() => setImportModalOpen(false)}
      >
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <Select
            aria-label={t('fields.dataSource')}
            placeholder={t('excelImport.selectDatasource')}
            options={dataSourceOptions}
            value={selectedDatasourceId}
            onChange={setSelectedDatasourceId}
            disabled={isBusy || Boolean(datasourceId)}
            showSearch
            optionFilterProp="label"
            style={{ width: '100%' }}
          />
          <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
            <Button
              icon={<DownloadOutlined />}
              loading={loadingAction === 'template'}
              disabled={isBusy && loadingAction !== 'template'}
              onClick={() => void handleTemplateDownload()}
            >
              {t('excelImport.downloadTemplate')}
            </Button>
          </div>
          <AntUpload.Dragger
            accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            maxCount={1}
            showUploadList={false}
            disabled={isBusy || !resolveDatasourceId()}
            beforeUpload={handleImport}
          >
            <p className="ant-upload-drag-icon">
              <UploadOutlined />
            </p>
            <p className="ant-upload-text">{t('excelImport.uploadText')}</p>
            <p className="ant-upload-hint">
              {loadingAction === 'import'
                ? t('excelImport.uploading')
                : t('excelImport.uploadHint')}
            </p>
          </AntUpload.Dragger>
        </Space>
      </Modal>
    </>
  );
};

export default SemanticExcelActions;
