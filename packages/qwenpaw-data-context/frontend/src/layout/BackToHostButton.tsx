import { message } from 'antd';
import { Button, RobotOutlined, Tooltip } from '@/design';
import {
  currentPageDatasourceId,
  getHostFrontendUrl,
  isAgentCenterSameApp,
  resolveAgentCenterUrl,
} from '@/auth';
import { setActiveDataSource } from '@/services/dataSource';
import { useAppI18n } from '@/i18n/useAppI18n';
import { useAuthStore } from '@/store/authStore';
import { useNavigate } from 'react-router-dom';
import { isEmbedded } from '../embedded/context';

interface BackToHostButtonProps {
  variant?: 'icon' | 'sider';
}

export default function BackToHostButton({ variant = 'icon' }: BackToHostButtonProps) {
  const navigate = useNavigate();
  const { t } = useAppI18n();
  const isPlatformAdmin = useAuthStore((state) => state.isPlatformAdmin);
  const label = variant === 'sider' ? t('layout.goToAgentCenter') : t('layout.goToAgentCenterFull');
  const hostUrl = getHostFrontendUrl();
  const agentCenterUrl = resolveAgentCenterUrl();

  // 未部署智能体中心（VITE_HOST_FRONTEND_URL 未配置）时不渲染：
  // 否则点击会 window.location.href = "" 而跳到空白页。
  if (!hostUrl) {
    return null;
  }

  return (
    <Tooltip title={isPlatformAdmin ? t('layout.agentCenterNeedsTenant') : label}>
      <Button
        type={variant === 'sider' ? 'primary' : 'text'}
        className={variant === 'sider' ? 'context-agent-entry-button' : undefined}
        aria-label={label}
        icon={<RobotOutlined />}
        href={variant === 'icon' ? agentCenterUrl : undefined}
        target={variant === 'icon' ? '_blank' : undefined}
        rel={variant === 'icon' ? 'noopener noreferrer' : undefined}
        onClick={(event) => {
          if (isEmbedded() && !isPlatformAdmin) {
            event.preventDefault();
            navigate('/console');
            return;
          }
          if (isPlatformAdmin && isAgentCenterSameApp(agentCenterUrl)) {
            event.preventDefault();
            message.info(t('layout.agentCenterNeedsTenant'));
            return;
          }
          const datasourceId = currentPageDatasourceId();
          if (datasourceId) {
            void setActiveDataSource(datasourceId).catch(() => undefined);
          }
          if (variant === 'icon') {
            return;
          }
          window.location.assign(agentCenterUrl);
        }}
      >
        {variant === 'sider' ? label : null}
      </Button>
    </Tooltip>
  );
}
