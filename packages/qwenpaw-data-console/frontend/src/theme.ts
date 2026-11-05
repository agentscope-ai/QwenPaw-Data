import { ConfigProvider } from '@agentscope-ai/design';
import type { ComponentProps } from 'react';

export const hostPalette = {
  primary: '#1677FF',
  primaryHover: '#4096FF',
  primaryActive: '#0958D9',
  primaryBg: '#E6F4FF',
  primaryBgHover: '#BAE0FF',
  primaryBorder: '#91CAFF',
  text: '#1F1F1F',
  textSecondary: '#595959',
  textTertiary: '#8C8C8C',
  textQuaternary: '#BFBFBF',
  layout: '#F5F7FA',
  container: '#FFFFFF',
  fill: '#F0F2F5',
  fillSecondary: '#F5F5F5',
  fillTertiary: '#F5F7FA',
  fillQuaternary: '#FAFAFA',
  border: '#D9D9D9',
  borderSecondary: '#F0F0F0',
  navigation: '#001529',
  success: '#389E0D',
  successBg: '#F6FFED',
  successBorder: '#B7EB8F',
  warning: '#FAAD14',
  warningBg: '#FFFBE6',
  warningBorder: '#FFE58F',
  error: '#FF4D4F',
  errorBg: '#FFF2F0',
  errorBorder: '#FFCCC7',
} as const;

export const hostTypography = {
  fontSize: 14,
  fontSizeSM: 12,
  fontSizeLG: 16,
  lineHeight: 1.6,
} as const;

export const hostGeometry = {
  borderRadius: 8,
  borderRadiusSM: 6,
  borderRadiusLG: 12,
  controlHeight: 36,
  controlHeightSM: 28,
  controlHeightLG: 40,
} as const;

export const hostThemeProviderProps: ComponentProps<typeof ConfigProvider> = {
  prefix: 'data-runtime',
  prefixCls: 'ant',
  theme: {
    cssVar: {
      key: 'datapaw-host',
      prefix: 'ant',
    },
    token: {
      colorPrimary: hostPalette.primary,
      colorPrimaryHover: hostPalette.primaryHover,
      colorPrimaryActive: hostPalette.primaryActive,
      colorPrimaryBg: hostPalette.primaryBg,
      colorPrimaryBgHover: hostPalette.primaryBgHover,
      colorPrimaryBorder: hostPalette.primaryBorder,
      colorTextBase: hostPalette.text,
      colorText: hostPalette.text,
      colorTextSecondary: hostPalette.textSecondary,
      colorTextTertiary: hostPalette.textTertiary,
      colorTextQuaternary: hostPalette.textQuaternary,
      colorTextDisabled: hostPalette.textQuaternary,
      colorTextLightSolid: hostPalette.container,
      colorBgBase: hostPalette.container,
      colorBgLayout: hostPalette.layout,
      colorBgContainer: hostPalette.container,
      colorBgElevated: hostPalette.container,
      colorBgSpotlight: hostPalette.navigation,
      colorFill: hostPalette.fill,
      colorFillSecondary: hostPalette.fillSecondary,
      colorFillTertiary: hostPalette.fillTertiary,
      colorFillQuaternary: hostPalette.fillQuaternary,
      colorBorder: hostPalette.border,
      colorBorderSecondary: hostPalette.borderSecondary,
      colorSplit: hostPalette.borderSecondary,
      colorSuccess: hostPalette.success,
      colorSuccessBg: hostPalette.successBg,
      colorSuccessBorder: hostPalette.successBorder,
      colorWarning: hostPalette.warning,
      colorWarningBg: hostPalette.warningBg,
      colorWarningBorder: hostPalette.warningBorder,
      colorError: hostPalette.error,
      colorErrorBg: hostPalette.errorBg,
      colorErrorBorder: hostPalette.errorBorder,
      colorInfo: hostPalette.primary,
      colorInfoBg: hostPalette.primaryBg,
      colorInfoBorder: hostPalette.primaryBorder,
      ...hostTypography,
      ...hostGeometry,
    },
    components: {
      Button: {
        borderRadius: hostGeometry.borderRadius,
        borderRadiusSM: hostGeometry.borderRadiusSM,
        controlHeight: hostGeometry.controlHeight,
        controlHeightSM: hostGeometry.controlHeightSM,
        contentFontSize: hostTypography.fontSize,
        contentFontSizeSM: hostTypography.fontSizeSM,
        defaultShadow: 'none',
        primaryShadow: 'none',
      },
      Collapse: {
        borderRadiusLG: hostGeometry.borderRadiusLG,
        contentBg: hostPalette.container,
        headerBg: hostPalette.fillQuaternary,
      },
      Input: {
        activeBorderColor: hostPalette.primary,
        hoverBorderColor: hostPalette.primaryHover,
        colorBgContainer: hostPalette.container,
      },
      Modal: {
        borderRadiusLG: hostGeometry.borderRadiusLG,
        contentBg: hostPalette.container,
        headerBg: hostPalette.container,
      },
      Segmented: {
        borderRadius: hostGeometry.borderRadius,
        itemSelectedBg: hostPalette.container,
        itemSelectedColor: hostPalette.primaryActive,
        trackBg: hostPalette.fillSecondary,
      },
      Select: {
        activeBorderColor: hostPalette.primary,
        hoverBorderColor: hostPalette.primaryHover,
        optionActiveBg: hostPalette.fillTertiary,
        optionSelectedBg: hostPalette.primaryBg,
        selectorBg: hostPalette.container,
      },
      Table: {
        borderColor: hostPalette.borderSecondary,
        headerBg: hostPalette.fillQuaternary,
        headerColor: hostPalette.textSecondary,
      },
      Tag: {
        borderRadiusSM: hostGeometry.borderRadiusSM,
        defaultBg: hostPalette.fillQuaternary,
      },
      Tooltip: {
        borderRadius: hostGeometry.borderRadiusSM,
        colorBgSpotlight: hostPalette.navigation,
      },
    },
  },
};

export const runtimeAntTheme = {
  ...hostThemeProviderProps.theme,
  cssVar: {
    key: 'datapaw-runtime',
    prefix: 'data-runtime',
  },
};

export const runtimeThemeOptions = {
  background: hostPalette.layout,
  narrowMode: false,
  prefix: 'data-runtime',
  locale: 'cn',
  typography: {
    baseFontSize: hostTypography.fontSize,
    baseLineHeight: hostTypography.lineHeight,
  },
} as const;
