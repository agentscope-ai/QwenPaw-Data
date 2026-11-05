import { capabilities } from '@/services/capabilities';
import {
  ApiOutlined,
  DatabaseOutlined,
  SettingOutlined,
} from '@ant-design/icons';
import { Button, Dropdown } from '@agentscope-ai/design';
import { useNavigate } from 'react-router-dom';
import {
  buildAgentConfigurationPath,
  buildChannelConfigurationPath,
} from '../model/sessionNavigation';
import { getContextFrontendUrl } from '@/services/config';
import styles from '../index.module.less';

interface ConsoleSettingsMenuProps {
  className?: string;
}

export default function ConsoleSettingsMenu({
  className,
}: ConsoleSettingsMenuProps) {
  const navigate = useNavigate();

  return (
    <div className={className}>
      <Dropdown
        placement="bottomRight"
        trigger={['hover']}
        menu={{
          items: [
            ...(capabilities.contextConsole ? [{ key: 'data-bridge', icon: <DatabaseOutlined />, label: '数据管理 · DataBridge' }] : []),
            { key: 'agent-configuration', icon: <SettingOutlined />, label: 'Agent Configuration' },
            ...(capabilities.channels ? [{ key: 'channel-configuration', icon: <ApiOutlined />, label: 'Channel Configuration' }] : []),
          ],
          onClick: ({ key }: { key: string }) => {
            if (key === 'data-bridge') {
              window.open(getContextFrontendUrl(), '_blank', 'noopener,noreferrer');
            }
            if (key === 'agent-configuration') {
              navigate(buildAgentConfigurationPath());
            }
            if (key === 'channel-configuration') {
              navigate(buildChannelConfigurationPath());
            }
          },
        }}
      >
        <Button
          className={styles.headerSettingsButton}
          type="text"
          icon={<SettingOutlined />}
          aria-label="设置"
        />
      </Dropdown>
    </div>
  );
}
