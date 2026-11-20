import tokens from '../tokens.json';

export {
  useQwenPawDataColorMode,
  type QwenPawDataColorMode,
} from './useQwenPawDataColorMode';
import type { QwenPawDataColorMode } from './useQwenPawDataColorMode';

export {
  QwenPawDataSiderAccountCard,
  type QwenPawDataSiderAccountAction,
  type QwenPawDataSiderAccountCardProps,
} from './SiderAccountCard';

export const dataPawPalette = {
  primary: tokens.color.primary.$value,
  primaryHover: tokens.color.primaryHover.$value,
  primaryActive: tokens.color.primaryActive.$value,
  primaryBg: tokens.color.primaryBg.$value,
  primaryBgHover: tokens.color.primaryBgHover.$value,
  primaryBorder: tokens.color.primaryBorder.$value,
  text: tokens.color.text.$value,
  textSecondary: tokens.color.textSecondary.$value,
  textTertiary: tokens.color.textTertiary.$value,
  textQuaternary: tokens.color.textQuaternary.$value,
  layout: tokens.color.layout.$value,
  container: tokens.color.container.$value,
  fill: tokens.color.fill.$value,
  fillSecondary: tokens.color.fillSecondary.$value,
  fillTertiary: tokens.color.fillTertiary.$value,
  fillQuaternary: tokens.color.fillQuaternary.$value,
  border: tokens.color.border.$value,
  borderSecondary: tokens.color.borderSecondary.$value,
  navigation: tokens.color.navigation.$value,
  success: tokens.color.success.$value,
  successBg: tokens.color.successBg.$value,
  successBorder: tokens.color.successBorder.$value,
  warning: tokens.color.warning.$value,
  warningBg: tokens.color.warningBg.$value,
  warningBorder: tokens.color.warningBorder.$value,
  error: tokens.color.error.$value,
  errorBg: tokens.color.errorBg.$value,
  errorBorder: tokens.color.errorBorder.$value,
} as const;

export const dataPawDarkPalette = {
  primary: tokens.colorDark.primary.$value,
  primaryHover: tokens.colorDark.primaryHover.$value,
  primaryActive: tokens.colorDark.primaryActive.$value,
  primaryBg: tokens.colorDark.primaryBg.$value,
  primaryBgHover: tokens.colorDark.primaryBgHover.$value,
  primaryBorder: tokens.colorDark.primaryBorder.$value,
  text: tokens.colorDark.text.$value,
  textSecondary: tokens.colorDark.textSecondary.$value,
  textTertiary: tokens.colorDark.textTertiary.$value,
  textQuaternary: tokens.colorDark.textQuaternary.$value,
  layout: tokens.colorDark.layout.$value,
  container: tokens.colorDark.container.$value,
  fill: tokens.colorDark.fill.$value,
  fillSecondary: tokens.colorDark.fillSecondary.$value,
  fillTertiary: tokens.colorDark.fillTertiary.$value,
  fillQuaternary: tokens.colorDark.fillQuaternary.$value,
  border: tokens.colorDark.border.$value,
  borderSecondary: tokens.colorDark.borderSecondary.$value,
  navigation: tokens.colorDark.navigation.$value,
  success: tokens.colorDark.success.$value,
  successBg: tokens.colorDark.successBg.$value,
  successBorder: tokens.colorDark.successBorder.$value,
  warning: tokens.colorDark.warning.$value,
  warningBg: tokens.colorDark.warningBg.$value,
  warningBorder: tokens.colorDark.warningBorder.$value,
  error: tokens.colorDark.error.$value,
  errorBg: tokens.colorDark.errorBg.$value,
  errorBorder: tokens.colorDark.errorBorder.$value,
} as const;

export const dataPawSurfaces = {
  route: tokens.surface.route.$value,
  drawer: tokens.surface.drawer.$value,
  form: tokens.surface.form.$value,
  tooltip: tokens.surface.tooltip.$value,
  loading: tokens.surface.loading.$value,
  inset: tokens.surface.inset.$value,
} as const;

export const dataPawDarkSurfaces = {
  route: tokens.surfaceDark.route.$value,
  drawer: tokens.surfaceDark.drawer.$value,
  form: tokens.surfaceDark.form.$value,
  tooltip: tokens.surfaceDark.tooltip.$value,
  loading: tokens.surfaceDark.loading.$value,
  inset: tokens.surfaceDark.inset.$value,
} as const;

export const dataPawTypography = {
  fontFamily: tokens.typography.fontFamily.$value,
  fontSize: tokens.typography.fontSize.$value,
  fontSizeSM: tokens.typography.fontSizeSM.$value,
  fontSizeLG: tokens.typography.fontSizeLG.$value,
  lineHeight: tokens.typography.lineHeight.$value,
} as const;

export const dataPawGeometry = {
  borderRadius: tokens.geometry.borderRadius.$value,
  borderRadiusSM: tokens.geometry.borderRadiusSM.$value,
  borderRadiusLG: tokens.geometry.borderRadiusLG.$value,
  borderRadiusTextArea: tokens.geometry.borderRadiusTextArea.$value,
  borderRadiusComposer: tokens.geometry.borderRadiusComposer.$value,
  borderRadiusPill: tokens.geometry.borderRadiusPill.$value,
  borderRadiusRoute: tokens.geometry.borderRadiusRoute.$value,
  borderRadiusCardPrimary: tokens.geometry.borderRadiusCardPrimary.$value,
  borderRadiusCardSecondary: tokens.geometry.borderRadiusCardSecondary.$value,
  borderRadiusOverlayLarge: tokens.geometry.borderRadiusOverlayLarge.$value,
  borderRadiusOverlaySmall: tokens.geometry.borderRadiusOverlaySmall.$value,
  controlHeight: tokens.geometry.controlHeight.$value,
  controlHeightSM: tokens.geometry.controlHeightSM.$value,
  controlHeightLG: tokens.geometry.controlHeightLG.$value,
} as const;

export const dataPawSpacing = {
  xxs: tokens.spacing.xxs.$value,
  xs: tokens.spacing.xs.$value,
  sm: tokens.spacing.sm.$value,
  md: tokens.spacing.md.$value,
  lg: tokens.spacing.lg.$value,
  xl: tokens.spacing.xl.$value,
  xxl: tokens.spacing.xxl.$value,
} as const;

export const dataPawLayout = {
  topbarHeight: tokens.layout.topbarHeight.$value,
  siderWidth: tokens.layout.siderWidth.$value,
  routeOuterMargin: tokens.layout.routeOuterMargin.$value,
  routeContentMinWidth: tokens.layout.routeContentMinWidth.$value,
  routeContentMaxWidth: tokens.layout.routeContentMaxWidth.$value,
} as const;

export const dataPawDensity = {
  compact: tokens.density.compact.$value,
  regular: tokens.density.regular.$value,
  spacious: tokens.density.spacious.$value,
} as const;

export const dataPawMotion = {
  fast: tokens.motion.fast.$value,
  collapse: tokens.motion.collapse.$value,
  layout: tokens.motion.layout.$value,
  workspace: tokens.motion.workspace.$value,
  emphasizedEasing: tokens.motion.emphasizedEasing.$value,
} as const;

export const dataPawElevation = {
  surface: tokens.elevation.surface.$value,
  cardHover: tokens.elevation.cardHover.$value,
  overlay: tokens.elevation.overlay.$value,
  routeCard: tokens.elevation.routeCard.$value,
  routeCardLarge: tokens.elevation.routeCardLarge.$value,
} as const;

type ThemeTokenMap = Record<string, unknown>;
type ThemeComponentMap = Record<string, ThemeTokenMap>;
type ThemeLike = Record<string, unknown> & {
  token?: ThemeTokenMap;
  components?: ThemeComponentMap;
};

export type QwenPawDataThemeOptions = {
  cssVarKey: string;
  cssVarPrefix?: string;
  colorMode?: QwenPawDataColorMode;
};

/** Builds shared Ant/AgentScope tokens while leaving product layout to the app. */
export function createQwenPawDataAntTheme(
  baseTheme: object,
  {
    cssVarKey,
    cssVarPrefix = 'ant',
    colorMode = 'light',
  }: QwenPawDataThemeOptions,
) {
  const normalizedTheme = baseTheme as ThemeLike;
  const baseComponents = normalizedTheme.components ?? {};
  const component = (name: string) => baseComponents[name] ?? {};
  const palette = colorMode === 'dark' ? dataPawDarkPalette : dataPawPalette;
  const surfaces = colorMode === 'dark' ? dataPawDarkSurfaces : dataPawSurfaces;
  const isDark = colorMode === 'dark';

  return {
    ...normalizedTheme,
    cssVar: {
      key: cssVarKey,
      prefix: cssVarPrefix,
    },
    token: {
      ...normalizedTheme.token,
      colorPrimary: palette.primary,
      colorPrimaryHover: palette.primaryHover,
      colorPrimaryActive: palette.primaryActive,
      colorPrimaryBg: palette.primaryBg,
      colorPrimaryBgHover: palette.primaryBgHover,
      colorPrimaryBorder: palette.primaryBorder,
      colorLink: palette.primaryActive,
      colorTextBase: palette.text,
      colorText: palette.text,
      colorTextSecondary: palette.textSecondary,
      colorTextTertiary: palette.textTertiary,
      colorTextQuaternary: palette.textQuaternary,
      colorTextDisabled: palette.textQuaternary,
      colorTextLightSolid: '#FFFFFF',
      colorBgBase: palette.layout,
      colorBgLayout: palette.layout,
      colorBgContainer: surfaces.route,
      colorBgElevated: surfaces.drawer,
      colorBgSpotlight: surfaces.tooltip,
      colorFill: palette.fill,
      colorFillSecondary: palette.fillSecondary,
      colorFillTertiary: palette.fillTertiary,
      colorFillQuaternary: palette.fillQuaternary,
      colorBorder: palette.border,
      colorBorderSecondary: palette.borderSecondary,
      colorSplit: palette.borderSecondary,
      colorSuccess: palette.success,
      colorSuccessBg: palette.successBg,
      colorSuccessBorder: palette.successBorder,
      colorWarning: palette.warning,
      colorWarningBg: palette.warningBg,
      colorWarningBorder: palette.warningBorder,
      colorError: palette.error,
      colorErrorBg: palette.errorBg,
      colorErrorBorder: palette.errorBorder,
      colorInfo: palette.primary,
      colorInfoBg: palette.primaryBg,
      colorInfoBorder: palette.primaryBorder,
      ...dataPawTypography,
      ...dataPawGeometry,
    },
    components: {
      ...baseComponents,
      Button: {
        ...component('Button'),
        borderRadius: dataPawGeometry.borderRadiusPill,
        borderRadiusSM: dataPawGeometry.borderRadiusPill,
        borderRadiusLG: dataPawGeometry.borderRadiusPill,
        controlHeight: dataPawGeometry.controlHeight,
        controlHeightSM: dataPawGeometry.controlHeightSM,
        controlHeightLG: dataPawGeometry.controlHeightLG,
        contentFontSize: dataPawTypography.fontSize,
        contentFontSizeSM: dataPawTypography.fontSizeSM,
        defaultShadow: 'none',
        primaryShadow: 'none',
      },
      Card: {
        ...component('Card'),
        borderRadiusLG: dataPawGeometry.borderRadiusCardPrimary,
        colorBgContainer: 'transparent',
        colorBorderSecondary: palette.border,
        headerBg: 'transparent',
        boxShadow: 'none',
      },
      Collapse: {
        ...component('Collapse'),
        borderRadiusLG: dataPawGeometry.borderRadiusLG,
        contentBg: surfaces.form,
        headerBg: palette.fillQuaternary,
      },
      Input: {
        ...component('Input'),
        activeBorderColor: palette.primary,
        borderRadius: dataPawGeometry.borderRadiusPill,
        borderRadiusLG: dataPawGeometry.borderRadiusPill,
        borderRadiusSM: dataPawGeometry.borderRadiusPill,
        hoverBorderColor: palette.primaryHover,
        colorBgContainer: surfaces.form,
      },
      Modal: {
        ...component('Modal'),
        borderRadiusLG: dataPawGeometry.borderRadiusOverlayLarge,
        boxShadow: dataPawElevation.overlay,
        contentBg: surfaces.drawer,
        footerBg: surfaces.drawer,
        headerBg: surfaces.drawer,
      },
      Drawer: {
        ...component('Drawer'),
        borderRadiusLG: dataPawGeometry.borderRadiusOverlayLarge,
        boxShadow: dataPawElevation.overlay,
        colorBgElevated: surfaces.drawer,
      },
      Dropdown: {
        ...component('Dropdown'),
        borderRadiusLG: dataPawGeometry.borderRadiusOverlaySmall,
        boxShadowSecondary: dataPawElevation.overlay,
        colorBgElevated: surfaces.drawer,
      },
      Popover: {
        ...component('Popover'),
        borderRadiusLG: dataPawGeometry.borderRadiusOverlaySmall,
        boxShadowSecondary: dataPawElevation.overlay,
        colorBgElevated: surfaces.drawer,
      },
      Pagination: {
        ...component('Pagination'),
        borderRadius: dataPawGeometry.borderRadiusPill,
        borderRadiusSM: dataPawGeometry.borderRadiusPill,
        itemActiveBg: palette.primaryBg,
        itemBg: 'transparent',
        itemLinkBg: 'transparent',
      },
      Tabs: {
        ...component('Tabs'),
        borderRadius: dataPawGeometry.borderRadiusPill,
        borderRadiusLG: dataPawGeometry.borderRadiusPill,
        cardBg: palette.fillTertiary,
        itemSelectedColor: palette.primaryActive,
      },
      Segmented: {
        ...component('Segmented'),
        borderRadius: dataPawGeometry.borderRadiusPill,
        borderRadiusSM: dataPawGeometry.borderRadiusPill,
        itemSelectedBg: isDark ? surfaces.loading : surfaces.form,
        itemSelectedColor: isDark ? palette.primaryHover : palette.primaryActive,
        trackBg: palette.fillSecondary,
      },
      Select: {
        ...component('Select'),
        activeBorderColor: palette.primary,
        borderRadius: dataPawGeometry.borderRadiusPill,
        borderRadiusLG: dataPawGeometry.borderRadiusPill,
        borderRadiusSM: dataPawGeometry.borderRadiusPill,
        hoverBorderColor: palette.primaryHover,
        optionActiveBg: palette.fillTertiary,
        optionSelectedBg: palette.primaryBg,
        selectorBg: surfaces.form,
      },
      Table: {
        ...component('Table'),
        borderColor: palette.borderSecondary,
        colorBgContainer: surfaces.route,
        colorText: palette.text,
        headerBg: isDark ? palette.fillSecondary : palette.fillQuaternary,
        headerColor: isDark ? palette.text : palette.textSecondary,
        rowHoverBg: palette.fillTertiary,
      },
      Tag: {
        ...component('Tag'),
        borderRadiusSM: dataPawGeometry.borderRadiusPill,
        defaultBg: palette.fillQuaternary,
      },
      Tooltip: {
        ...component('Tooltip'),
        borderRadius: dataPawGeometry.borderRadiusOverlaySmall,
        boxShadowSecondary: dataPawElevation.overlay,
        colorBgSpotlight: surfaces.tooltip,
      },
    },
  };
}
