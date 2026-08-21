import { Col, Row } from '@/design';
import React, { useEffect } from 'react';
import { useNavigate } from 'react-router';

// 组件
import {
  BottomButtons,
  InputInstructionsCard,
  MetricTable,
  QuickActionsCard,
  StepsCard,
} from './components';
import MetricCheckModal from './components/MetricCheckModal';

// Hooks
import { useModal } from '@/hooks/useModal';
import { useMetricAdd } from './useMetricAdd';

// 工具函数
import { downloadExcelTemplate, parseExcelFile } from './utils/excelUtils';
import { useTranslation } from 'react-i18next';

const MetricAdd: React.FC = () => {
  const { t } = useTranslation();
  const navigate = useNavigate();

  // 从自定义 hook 获取状态和方法
  const {
    currentStep,
    dataSource,
    confirmData,
    editableKeys,
    prevStep,
    setDataSource,
    setConfirmData,
    setEditableKeys,
    addRow,
    deleteRow,
    clearTable,
    importData,
    consistencyCheck,
    confirmCreate,
    handleConfirmCreateLib,
    reset,
  } = useMetricAdd();

  // 根据当前步骤处理删除行
  const handleDeleteRow = (id: string) => {
    if (currentStep < 2) {
      deleteRow(id);
    } else {
      setConfirmData((prev) => prev.filter((item) => item.id !== id));
      setEditableKeys((prev) => prev.filter((key) => key !== id));
    }
  };

  // 弹窗 Hook
  const { modal: metricCheckModal, showModal: showMetricCheckModal } = useModal(MetricCheckModal);

  // 组件卸载时重置状态
  useEffect(() => {
    return () => {
      reset();
    };
  }, [reset]);

  // 处理一致性检查
  const handleConsistencyCheck = async () => {
    const result = await consistencyCheck(dataSource);
    if (result.hasDuplicate) {
      showMetricCheckModal({
        title: t('metricAdd.duplicateTitle'),
        data: result.data,
        callback: handleConfirmMetricCreate,
      });
    }
    // 无重复时 hook 内部已调用 nextStep
  };

  // 确认创建指标（弹窗回调） type 默认是按钮
  const handleConfirmMetricCreate = (type: 'button' | 'modal' = 'button') => {
    confirmCreate(dataSource, type);
  };

  // 从模板导入（使用 Excel 解析）
  const handleImport = async (file: File) => {
    try {
      const newRows = await parseExcelFile(file);
      if (newRows.length > 0) {
        importData(newRows);
      }
    } catch {
      // parseExcelFile 内部已提示错误
    }
    return false; // 阻止默认上传行为
  };

  // 取消
  const handleCancel = () => {
    navigate('/metric-lib');
  };

  return (
    <div style={{ padding: 24, background: '#f5f5f5', minHeight: '100vh' }}>
      {/* 步骤条 */}
      <StepsCard currentStep={currentStep} />

      <Row gutter={[16, 16]}>
        {/* 左侧区域 */}
        <Col xs={24} sm={24} md={8} lg={6}>
          {/* 录入说明 */}
          <InputInstructionsCard />

          {/* 快捷操作 */}
          <QuickActionsCard
            onAddRow={addRow}
            onClearTable={clearTable}
            onImport={handleImport}
            onDownloadTemplate={downloadExcelTemplate}
            currentStep={currentStep}
          />

          {/* 底部操作按钮 */}
          <BottomButtons
            onConfirmCreateLib={handleConfirmCreateLib}
            currentStep={currentStep}
            onPrevStep={prevStep}
            onConsistencyCheck={handleConsistencyCheck}
            onConfirmCreate={() => confirmCreate(dataSource, 'button')}
            onCancel={handleCancel}
            isCheckDisabled={dataSource.length === 0}
            isConfirmDisabled={currentStep < 1}
          />
        </Col>

        {/* 右侧表格区域 */}
        <Col xs={24} sm={24} md={16} lg={18}>
          <MetricTable
            dataSource={currentStep < 2 ? dataSource : confirmData}
            editableKeys={editableKeys}
            onDataChange={currentStep < 2 ? setDataSource : setConfirmData}
            onEditableKeysChange={setEditableKeys}
            onDeleteRow={handleDeleteRow}
            mode={currentStep < 2 ? 'simple' : 'full'}
          />
        </Col>
      </Row>
      {metricCheckModal}
    </div>
  );
};

export default MetricAdd;
