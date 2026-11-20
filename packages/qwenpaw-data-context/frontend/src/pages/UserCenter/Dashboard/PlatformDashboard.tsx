import React, { useCallback, useEffect, useState } from 'react';
import { Button, Card, List, Skeleton, Space, Tag, message } from 'antd';
import { BankOutlined, PlusOutlined, RightOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { authFetch } from '@/auth';
import type { Tenant } from '@/auth';
import StatCard from '@/pages/UserCenter/components/StatCard';
import { getErrorMessage } from '@/services/apiError';

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

type TenantItem = Tenant;

/* ------------------------------------------------------------------ */
/*  Status tag colors                                                  */
/* ------------------------------------------------------------------ */

const STATUS_TAG: Record<string, { color: string; label?: string }> = {
  active: { color: 'success' },
  suspended: { color: 'default' },
  pending: { color: 'warning' },
  queued: { color: 'warning' },
  provisioning: { color: 'processing' },
  ready: { color: 'success' },
  failed: { color: 'error' },
};

/* ------------------------------------------------------------------ */
/*  Styles                                                             */
/* ------------------------------------------------------------------ */

const PAGE_STYLE: React.CSSProperties = {
  padding: '4px 0 24px',
};

const SECTION_TITLE: React.CSSProperties = {
  fontSize: 18,
  fontWeight: 600,
  color: '#1e293b',
  marginBottom: 16,
  fontFamily: "'Inter', 'Noto Sans SC', sans-serif",
};

const STAT_GRID: React.CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))',
  gap: 16,
  marginBottom: 32,
};

/* ------------------------------------------------------------------ */
/*  Component                                                          */
/* ------------------------------------------------------------------ */

const PlatformDashboard: React.FC = () => {
  const { t } = useTranslation('translation', { keyPrefix: 'auth' });
  const navigate = useNavigate();
  const [tenants, setTenants] = useState<TenantItem[]>([]);
  const [loading, setLoading] = useState(true);

  const loadTenants = useCallback(async () => {
    try {
      const result = await authFetch<{ tenants: TenantItem[] }>('/api/platform/tenants');
      setTenants(result.tenants ?? []);
    } catch (err: unknown) {
      message.error(getErrorMessage(err, t('common.loadFailed')));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    loadTenants();
  }, [loadTenants]);

  /* Computed stats */
  const totalTenants = tenants.length;

  const activeTenants = tenants.filter((tenant) => tenant.status === 'active').length;

  const pendingProvision = tenants.filter((tenant) => {
    const is = tenant.instance_status;
    return is === 'pending' || is === 'queued' || is === 'provisioning';
  }).length;

  const recentTenants = tenants.slice(-5).reverse();

  return (
    <div style={PAGE_STYLE}>
      {/* Stat cards */}
      {loading ? (
        <div style={STAT_GRID}>
          {[1, 2, 3].map((k) => (
            <Skeleton.Node key={k} active style={{ width: '100%', height: 88 }} />
          ))}
        </div>
      ) : (
        <div style={STAT_GRID}>
          <StatCard
            icon={<BankOutlined />}
            label={t('dashboard.totalTenants')}
            value={totalTenants}
          />
          <StatCard
            icon={<BankOutlined />}
            label={t('dashboard.activeTenants')}
            value={activeTenants}
            color="#16a34a"
          />
          <StatCard
            icon={<BankOutlined />}
            label={t('dashboard.pendingProvision')}
            value={pendingProvision}
            color="#d97706"
          />
        </div>
      )}

      {/* Quick Actions */}
      <h2 style={SECTION_TITLE}>{t('dashboard.quickActions')}</h2>
      <Space style={{ marginBottom: 32 }}>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => navigate('/user-center/tenants')}
          style={{ borderRadius: 12 }}
        >
          {t('dashboard.createTenant')}
        </Button>
      </Space>

      {/* Recent Tenants */}
      <h2 style={SECTION_TITLE}>{t('dashboard.recentTenants')}</h2>
      <Card style={{ borderRadius: 12 }} styles={{ body: { padding: 0 } }}>
        <List
          loading={loading}
          dataSource={recentTenants}
          locale={{ emptyText: t('common.noData') }}
          renderItem={(tenant) => {
            const tagInfo = STATUS_TAG[tenant.status] ?? { color: 'default' };
            return (
              <List.Item
                style={{ padding: '12px 24px' }}
                actions={[
                  <Button
                    key="view"
                    type="link"
                    size="small"
                    icon={<RightOutlined />}
                    onClick={() => navigate('/user-center/tenants')}
                  />,
                ]}
              >
                <List.Item.Meta
                  title={
                    <Space>
                      <span style={{ fontWeight: 500, color: '#1e293b' }}>{tenant.name}</span>
                      <Tag color={tagInfo.color}>{t(`status.${tenant.status}`)}</Tag>
                      {tenant.instance_status && (
                        <Tag
                          color={(STATUS_TAG[tenant.instance_status] ?? { color: 'default' }).color}
                        >
                          {t(`status.${tenant.instance_status}`)}
                        </Tag>
                      )}
                    </Space>
                  }
                  description={
                    <span style={{ fontSize: 12, color: '#94a3b8' }}>ID: {tenant.id}</span>
                  }
                />
              </List.Item>
            );
          }}
        />
      </Card>
    </div>
  );
};

export default PlatformDashboard;
