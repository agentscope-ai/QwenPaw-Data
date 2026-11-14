import React from 'react';
import { Button, Result, Space } from 'antd';
import { useNavigate } from 'react-router-dom';
import type { Resource } from '@/auth';
import { useTranslation } from 'react-i18next';

const RESOURCE_LABELS: Partial<Record<Resource, string>> = {
  semantic_weaving: '语义编织',
  excel_import: 'Excel 导入',
  model_config: '模型配置',
  'datasource:write': '数据源管理',
  'datasource:read': '数据源',
  'mg:read': '图谱可视化',
  'mg:write': '全局主图谱编辑',
  'mg_userspace:sync': '个人元数据图同步',
  'mg_userspace:append': '个人元数据图追加',
  raw_cypher: 'Raw Cypher',
  'tg_kg:self': 'KG 文档',
};

interface Props {
  resource?: Resource;
}

const NoPermissionPage: React.FC<Props> = ({ resource }) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const label = resource ? RESOURCE_LABELS[resource] : undefined;

  return (
    <div
      style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: 400 }}
    >
      <Result
        status="403"
        title={t('noPermission.title')}
        subTitle={`${t('noPermission.notAuthorizedToAccess')}${label ? `:「${label}」。` : '。'}${t('noPermission.contactWorkspaceAdmin')}`}
        extra={
          <Space direction="vertical" size={12} style={{ maxWidth: 480 }}>
            <Button type="primary" onClick={() => navigate('/')}>
              {t('noPermission.returnToHome')}
            </Button>
          </Space>
        }
      />
    </div>
  );
};

export default NoPermissionPage;
