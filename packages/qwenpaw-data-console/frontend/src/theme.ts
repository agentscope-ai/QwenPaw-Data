import { purpleTheme, ConfigProvider } from '@agentscope-ai/design';
import {
  createQwenPawDataAntTheme,
  dataPawDarkPalette,
  dataPawGeometry,
  dataPawPalette,
  dataPawTypography,
  type QwenPawDataColorMode,
} from '@qwenpaw-data/ui-tokens';
import type { ComponentProps } from 'react';
import { theme as antdTheme } from 'antd';

export const hostPalette = dataPawPalette;
export const hostTypography = dataPawTypography;
export const hostGeometry = dataPawGeometry;

const baseTheme = purpleTheme.theme ?? {};
type ConfigTheme = NonNullable<ComponentProps<typeof ConfigProvider>['theme']>;

export function createHostThemeProviderProps(
  colorMode: QwenPawDataColorMode,
): ComponentProps<typeof ConfigProvider> {
  const theme = createQwenPawDataAntTheme(baseTheme, {
    cssVarKey: 'qwenpaw-data-host',
    colorMode,
  }) as ConfigTheme;

  return {
    ...purpleTheme,
    prefix: 'data-runtime',
    prefixCls: 'ant',
    theme: {
      ...theme,
      algorithm: colorMode === 'dark' ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
    },
  };
}

export function createRuntimeAntTheme(colorMode: QwenPawDataColorMode) {
  const hostTheme = createHostThemeProviderProps(colorMode).theme;
  return {
    ...hostTheme,
    cssVar: {
      key: 'qwenpaw-data-runtime',
      prefix: 'data-runtime',
    },
  };
}

export function createRuntimeThemeOptions(colorMode: QwenPawDataColorMode) {
  const palette = colorMode === 'dark' ? dataPawDarkPalette : dataPawPalette;
  return {
    background: palette.layout,
    narrowMode: false,
    prefix: 'data-runtime',
    locale: 'cn',
    typography: {
      baseFontSize: hostTypography.fontSize,
      baseLineHeight: hostTypography.lineHeight,
    },
  } as const;
}
