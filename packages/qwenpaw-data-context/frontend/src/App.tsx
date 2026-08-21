import React from 'react';
import { ConfigProvider, baseTheme } from '@/design';
import enUS from 'antd/locale/en_US';
import zhCN from 'antd/locale/zh_CN';
import { RouterProvider } from 'react-router';
import router from './router';
import './i18n';
import { useAppI18n } from './i18n/useAppI18n';
import {
  BRAND_PRIMARY,
  BRAND_PRIMARY_ACTIVE,
  BRAND_PRIMARY_BG,
  BRAND_PRIMARY_HOVER,
} from './brand';
import AuthGate from './AuthGate';

const qwenpawDataTheme = {
  ...baseTheme,
  theme: {
    ...baseTheme.theme,
    token: {
      ...baseTheme.theme?.token,
      colorPrimary: BRAND_PRIMARY,
      colorPrimaryHover: BRAND_PRIMARY_HOVER,
      colorPrimaryActive: BRAND_PRIMARY_ACTIVE,
      colorPrimaryBg: BRAND_PRIMARY_BG,
      colorLink: BRAND_PRIMARY_ACTIVE,
    },
  },
};

const App: React.FC = React.memo(() => {
  const { language } = useAppI18n();
  const antdLocale = language === 'zh' ? zhCN : enUS;

  return (
    <ConfigProvider {...qwenpawDataTheme} locale={antdLocale}>
      <AuthGate>
        <RouterProvider router={router} />
      </AuthGate>
    </ConfigProvider>
  );
});

export default App;
