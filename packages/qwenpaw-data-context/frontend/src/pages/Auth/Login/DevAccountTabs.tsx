import React, { useState } from 'react';
import { Tabs, Typography } from 'antd';
import type { FormInstance } from 'antd';
import { useTranslation } from 'react-i18next';

import styles from './DevAccountTabs.module.less';

/**
 * 走查账号快填条：点击 Tab 把账号写入登录表单，省去手输。
 *
 * 口令不落源码，取本地 .env 的 `VITE_DEV_LOGIN_PASSWORD`，vite.config.ts 在构建时
 * 把该值强制置空（口令不进包）。登录页只在 dev server（import.meta.env.DEV）渲染本组件：
 *   • 已配置口令：回填用户名 + 口令，一键登录；
 *   • 未配置口令：仅回填用户名，口令手输（提示文案 missingPassword）。
 * 账号清单是本地联调用的双租户四角色示例账号，需要先在本地自行创建。
 */

interface DevAccount {
  /** 登录用户名 */
  username: string;
  /** 租户 / 工作空间归属 */
  scope: string;
  /** 色彩标识：每个账号一个色相，方便快速区分 */
  hue: HueName;
}

type HueName = 'red' | 'purple' | 'blue' | 'green' | 'cyan' | 'orange' | 'magenta';

/**
 * 色卡：用 antd 调色板主色作文字色（浅/深背景下都可读），
 * 背景用同色低透明度，保证“淡淡的底色”且无需区分亮/暗主题。
 */
const HUES: Record<HueName, { bg: string; activeBg: string; fg: string }> = {
  red: { bg: 'rgba(255, 77, 79, 0.10)', activeBg: 'rgba(255, 77, 79, 0.24)', fg: '#ff4d4f' },
  purple: { bg: 'rgba(146, 84, 222, 0.10)', activeBg: 'rgba(146, 84, 222, 0.24)', fg: '#9254de' },
  blue: { bg: 'rgba(22, 119, 255, 0.10)', activeBg: 'rgba(22, 119, 255, 0.24)', fg: '#1677ff' },
  green: { bg: 'rgba(82, 196, 26, 0.12)', activeBg: 'rgba(82, 196, 26, 0.28)', fg: '#52c41a' },
  cyan: { bg: 'rgba(19, 194, 194, 0.12)', activeBg: 'rgba(19, 194, 194, 0.28)', fg: '#13c2c2' },
  orange: { bg: 'rgba(250, 140, 22, 0.12)', activeBg: 'rgba(250, 140, 22, 0.28)', fg: '#fa8c16' },
  magenta: { bg: 'rgba(235, 47, 150, 0.10)', activeBg: 'rgba(235, 47, 150, 0.24)', fg: '#eb2f96' },
};

interface DevAccountGroup {
  /** 角色（分组维度） */
  role: string;
  accounts: DevAccount[];
}

/** 按角色分组，权限从高到低。platform-root 走 /platform/login，不在此列。 */
const ACCOUNT_GROUPS: DevAccountGroup[] = [
  {
    role: 'tenant_admin',
    accounts: [
      { username: 'acme-admin', scope: 'Acme', hue: 'red' },
      { username: 'globex-admin', scope: 'Globex', hue: 'purple' },
    ],
  },
  {
    role: 'ws_admin',
    accounts: [{ username: 'acme-ws1-admin', scope: 'Acme / WS-1', hue: 'blue' }],
  },
  {
    role: 'member',
    accounts: [
      { username: 'alice', scope: 'Acme / WS-1', hue: 'green' },
      { username: 'bob', scope: 'Acme / WS-1', hue: 'cyan' },
      { username: 'carol', scope: 'Acme / WS-2', hue: 'orange' },
      { username: 'dave', scope: 'Globex / GX-1', hue: 'magenta' },
    ],
  },
];

const DEV_PASSWORD = import.meta.env.VITE_DEV_LOGIN_PASSWORD ?? '';

/**
 * 个别账号的口令覆盖，格式 `user:pass,user2:pass2`（取第一个冒号分隔）。
 * 用于密码被单独改过、不同于统一口令的账号，避免为了对齐口令去重置密码
 * （管理员重置会置 force_password_change，下次登录被迫改密）。
 */
const DEV_PASSWORD_OVERRIDES: Record<string, string> = (
  import.meta.env.VITE_DEV_LOGIN_PASSWORD_OVERRIDES ?? ''
)
  .split(',')
  .map((pair) => pair.trim())
  .filter(Boolean)
  .reduce<Record<string, string>>((acc, pair) => {
    const separator = pair.indexOf(':');
    if (separator > 0) {
      acc[pair.slice(0, separator).trim()] = pair.slice(separator + 1);
    }
    return acc;
  }, {});

const passwordFor = (username: string) => DEV_PASSWORD_OVERRIDES[username] ?? DEV_PASSWORD;

interface DevAccountTabsProps {
  form: FormInstance<{ username: string; password: string }>;
}

const DevAccountTabs: React.FC<DevAccountTabsProps> = ({ form }) => {
  const { t } = useTranslation('translation', { keyPrefix: 'auth.login.devFill' });
  const [activeKey, setActiveKey] = useState<string>('');

  const findAccount = (username: string) => {
    for (const group of ACCOUNT_GROUPS) {
      const hit = group.accounts.find((item) => item.username === username);
      if (hit) {
        return { ...hit, role: group.role };
      }
    }
    return undefined;
  };

  const handleSelect = (username: string) => {
    const account = findAccount(username);
    if (!account) {
      return;
    }
    setActiveKey(username);
    const password = passwordFor(account.username);
    // 生产构建里口令被强制置空，此时只回填用户名。
    // 不能无条件写 password：空串会把使用者已经手输的口令洗掉。
    form.setFieldsValue(
      password ? { username: account.username, password } : { username: account.username },
    );
  };

  const selected = activeKey ? findAccount(activeKey) : undefined;
  const selectedPassword = selected ? passwordFor(selected.username) : DEV_PASSWORD;

  return (
    <div className={styles.wrapper}>
      <Typography.Text type="secondary" className={styles.caption}>
        {t('label')}
      </Typography.Text>

      {ACCOUNT_GROUPS.map((group) => (
        <div className={styles.group} key={group.role}>
          <span className={styles.groupLabel}>{group.role}</span>
          <Tabs
            className={styles.tabs}
            size="small"
            activeKey={activeKey}
            onChange={handleSelect}
            tabBarGutter={8}
            items={group.accounts.map((account) => {
              const hue = HUES[account.hue];
              const isActive = account.username === activeKey;
              return {
                key: account.username,
                label: (
                  <span
                    className={styles.pill}
                    style={{
                      background: isActive ? hue.activeBg : hue.bg,
                      borderColor: isActive ? hue.fg : 'transparent',
                      color: hue.fg,
                    }}
                  >
                    {account.username}
                  </span>
                ),
              };
            })}
          />
        </div>
      ))}

      <Typography.Text type={selectedPassword ? 'secondary' : 'warning'} className={styles.caption}>
        {!selectedPassword
          ? t('missingPassword')
          : selected
            ? t('filled', {
                username: selected.username,
                role: selected.role,
                scope: selected.scope,
              })
            : t('hint')}
      </Typography.Text>
    </div>
  );
};

export default DevAccountTabs;
