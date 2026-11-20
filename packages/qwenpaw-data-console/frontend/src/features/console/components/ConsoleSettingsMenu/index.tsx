import {
  ApiOutlined,
  BarChartOutlined,
  CloudServerOutlined,
  SettingOutlined,
} from '@ant-design/icons';
import { Button, Dropdown } from '@agentscope-ai/design';
import { useNavigate } from 'react-router-dom';
import {
  buildAgentConfigurationPath,
  buildChannelConfigurationPath,
  buildMcpConfigurationPath,
  buildUsagePath,
} from '@/shared/navigation/sessionNavigation';
import { useAppI18n } from '@/i18n';
import sharedStyles from '../../index.module.less';
import { mergeStyleModules } from '../../styleModules';
import ownerStyles from './index.module.less';

const styles = mergeStyleModules(ownerStyles, sharedStyles);

function getSettingsMenuPopupContainer(triggerNode: HTMLElement): HTMLElement {
  return triggerNode.closest<HTMLElement>('[data-qwenpaw-data-sider-account-card]') ?? document.body;
}

interface ConsoleSettingsMenuProps {
  className?: string;
  buttonClassName?: string;
  onAction?: () => void;
}

export default function ConsoleSettingsMenu({
  className,
  buttonClassName,
  onAction,
}: ConsoleSettingsMenuProps) {
  const navigate = useNavigate();
  const { t } = useAppI18n();

  return (
    <div className={className}>
      <Dropdown
        getPopupContainer={getSettingsMenuPopupContainer}
        mouseEnterDelay={0.08}
        mouseLeaveDelay={0.16}
        overlayClassName={styles.workspaceSettingsDropdown}
        placement="topRight"
        trigger={['hover']}
        menu={{
          items: [
            {
              key: 'agent-configuration',
              icon: <SettingOutlined />,
              label: t('settings.agentConfiguration'),
            },
            {
              key: 'channel-configuration',
              icon: <ApiOutlined />,
              label: t('settings.channelConfiguration'),
            },
            {
              key: 'mcp-configuration',
              icon: <CloudServerOutlined />,
              label: t('settings.mcpConfiguration'),
            },
            { key: 'usage', icon: <BarChartOutlined />, label: t('settings.usage') },
          ],
          onClick: ({ key }: { key: string }) => {
            if (key === 'agent-configuration') {
              navigate(buildAgentConfigurationPath());
              onAction?.();
            }
            if (key === 'channel-configuration') {
              navigate(buildChannelConfigurationPath());
              onAction?.();
            }
            if (key === 'mcp-configuration') {
              navigate(buildMcpConfigurationPath());
              onAction?.();
            }
            if (key === 'usage') {
              navigate(buildUsagePath());
              onAction?.();
            }
          },
        }}
      >
        <Button
          className={buttonClassName}
          type="text"
          icon={<SettingOutlined />}
          aria-label={t('common.settings')}
          title={t('common.settings')}
        />
      </Dropdown>
    </div>
  );
}
