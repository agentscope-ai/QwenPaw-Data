/** Graph Center (图谱中心) — React port of cm-graph-demo.
 *  Routes under /graph-center:
 *    /graph-center            → replace-redirect to /graph-center/global (keep search)
 *    /graph-center/dashboard  → dashboard · data overview (+ health issues)
 *    /graph-center/entity/:id → entity detail · lineage
 *    /graph-center/audit      → redirect to dashboard#health
 *    /graph-center/global     → global graph + left search panel
 *  The demo's own sidebar nav is provided by the app layout menu instead. */
import { Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Segmented } from 'antd';
import { BarChartOutlined, GlobalOutlined } from '@ant-design/icons';
import { ConfigProvider } from '@/design';
import LayerLegendPopover from './components/LayerLegend';
import { iosMotionTheme } from './components/iosMotion';
import pageMotion from './components/iosMotion.module.less';
import EntityDetailPage from './pages/EntityDetailPage';
import ViewerSwitcher from './components/ViewerSwitcher';
import GlobalGraphPage from './pages/GlobalGraphPage';
import DashboardPage from './pages/DashboardPage';
import { GraphCenterProvider } from './GraphCenterContext';
import styles from './index.module.less';

function RedirectToGlobal() {
  const { search } = useLocation();
  return <Navigate to={`/graph-center/global${search}`} replace />;
}

export default function GraphCenter() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();

  const tabs = [
    {
      key: 'dashboard',
      path: '/graph-center/dashboard',
      label: t('graphCenter.tabDashboard'),
      icon: <BarChartOutlined />,
    },
    {
      key: 'global',
      path: '/graph-center/global',
      label: t('graphCenter.tabGlobal'),
      icon: <GlobalOutlined />,
    },
  ];

  const activeKey = location.pathname.startsWith('/graph-center/dashboard')
    ? 'dashboard'
    : location.pathname.startsWith('/graph-center/global')
      ? 'global'
      : '';

  return (
    <ConfigProvider theme={iosMotionTheme}>
      <GraphCenterProvider>
        <div className={styles.shell}>
          <div className={styles.tabsBar}>
            <Segmented<string>
              className={styles.graphTabs}
              value={activeKey}
              options={tabs.map((tab) => ({
                value: tab.key,
                label: (
                  <span className={styles.graphTabLabel}>
                    {tab.icon}
                    {tab.label}
                  </span>
                ),
              }))}
              onChange={(key) => {
                const nextTab = tabs.find((tab) => tab.key === key);
                if (nextTab) navigate(nextTab.path);
              }}
            />
          </div>
          <div className={styles.viewerLegendBar}>
            <ViewerSwitcher />
            <LayerLegendPopover className={styles.legendTriggerShell} />
          </div>
          <div className={`${styles.view} ${activeKey === 'global' ? styles.viewFill : ''}`}>
            <div key={activeKey || 'detail'} className={`${styles.pagePane} ${pageMotion.pageIn}`}>
              <Routes>
                <Route index element={<RedirectToGlobal />} />
                <Route path="dashboard" element={<DashboardPage />} />
                <Route path="entity/:id" element={<EntityDetailPage />} />
                <Route path="trace/:taskKey" element={<RedirectToGlobal />} />
                <Route
                  path="audit"
                  element={<Navigate to="/graph-center/dashboard#health" replace />}
                />
                <Route path="global" element={<GlobalGraphPage />} />
                <Route path="*" element={<RedirectToGlobal />} />
              </Routes>
            </div>
          </div>
        </div>
      </GraphCenterProvider>
    </ConfigProvider>
  );
}
