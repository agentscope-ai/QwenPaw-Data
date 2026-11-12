import {
  createQwenPawDataAntTheme,
  dataPawGeometry,
  dataPawPalette,
  dataPawTypography,
  type QwenPawDataColorMode,
} from '@qwenpaw-data/ui-tokens';
import type { ComponentProps } from 'react';
import { theme as antdTheme } from 'antd';
import { purpleTheme, ConfigProvider } from '@/design';

export const contextPalette = dataPawPalette;
export const contextTypography = dataPawTypography;
export const contextGeometry = dataPawGeometry;

const baseTheme = purpleTheme.theme ?? {};
type ConfigTheme = NonNullable<ComponentProps<typeof ConfigProvider>['theme']>;

export function createContextThemeProviderProps(
  colorMode: QwenPawDataColorMode,
): ComponentProps<typeof ConfigProvider> {
  const theme = createQwenPawDataAntTheme(baseTheme, {
    cssVarKey: 'qwenpaw-data-context',
    colorMode,
  }) as ConfigTheme;

  return {
    ...purpleTheme,
    theme: {
      ...theme,
      algorithm: colorMode === 'dark' ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
    },
  };
}
