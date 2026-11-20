import { readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

const packageRoot = fileURLToPath(new URL('../', import.meta.url));
const tokensPath = `${packageRoot}tokens.json`;
const outputPath = `${packageRoot}src/tokens.css`;
const tokens = JSON.parse(await readFile(tokensPath, 'utf8'));
const value = (group, name) => tokens[group][name].$value;
const px = (group, name) => `${value(group, name)}px`;
const colorVariables = (group) => `  --qwenpaw-data-color-primary: ${value(group, 'primary')};
  --qwenpaw-data-color-primary-hover: ${value(group, 'primaryHover')};
  --qwenpaw-data-color-primary-active: ${value(group, 'primaryActive')};
  --qwenpaw-data-color-primary-bg: ${value(group, 'primaryBg')};
  --qwenpaw-data-color-primary-bg-hover: ${value(group, 'primaryBgHover')};
  --qwenpaw-data-color-primary-border: ${value(group, 'primaryBorder')};
  --qwenpaw-data-color-text: ${value(group, 'text')};
  --qwenpaw-data-color-text-secondary: ${value(group, 'textSecondary')};
  --qwenpaw-data-color-text-tertiary: ${value(group, 'textTertiary')};
  --qwenpaw-data-color-text-quaternary: ${value(group, 'textQuaternary')};
  --qwenpaw-data-color-layout: ${value(group, 'layout')};
  --qwenpaw-data-color-container: ${value(group, 'container')};
  --qwenpaw-data-color-fill: ${value(group, 'fill')};
  --qwenpaw-data-color-fill-secondary: ${value(group, 'fillSecondary')};
  --qwenpaw-data-color-fill-tertiary: ${value(group, 'fillTertiary')};
  --qwenpaw-data-color-fill-quaternary: ${value(group, 'fillQuaternary')};
  --qwenpaw-data-color-border: ${value(group, 'border')};
  --qwenpaw-data-color-border-secondary: ${value(group, 'borderSecondary')};
  --qwenpaw-data-color-navigation: ${value(group, 'navigation')};`;
const surfaceVariables = (group) => `  --qwenpaw-data-color-route-surface: ${value(group, 'route')};
  --qwenpaw-data-color-drawer-surface: ${value(group, 'drawer')};
  --qwenpaw-data-color-form-surface: ${value(group, 'form')};
  --qwenpaw-data-color-tooltip-surface: ${value(group, 'tooltip')};
  --qwenpaw-data-color-loading-surface: ${value(group, 'loading')};
  --qwenpaw-data-color-inset-surface: ${value(group, 'inset')};`;

const css = `:root {
${colorVariables('color')}
${surfaceVariables('surface')}
  --qwenpaw-data-font-family: ${value('typography', 'fontFamily')};
  --qwenpaw-data-font-size-sm: ${px('typography', 'fontSizeSM')};
  --qwenpaw-data-font-size: ${px('typography', 'fontSize')};
  --qwenpaw-data-font-size-lg: ${px('typography', 'fontSizeLG')};
  --qwenpaw-data-line-height: ${value('typography', 'lineHeight')};
  --qwenpaw-data-radius-sm: ${px('geometry', 'borderRadiusSM')};
  --qwenpaw-data-radius: ${px('geometry', 'borderRadius')};
  --qwenpaw-data-radius-lg: ${px('geometry', 'borderRadiusLG')};
  --qwenpaw-data-radius-textarea: ${px('geometry', 'borderRadiusTextArea')};
  --qwenpaw-data-radius-composer: ${px('geometry', 'borderRadiusComposer')};
  --qwenpaw-data-radius-pill: ${px('geometry', 'borderRadiusPill')};
  --qwenpaw-data-radius-route: ${px('geometry', 'borderRadiusRoute')};
  --qwenpaw-data-radius-card-primary: ${px('geometry', 'borderRadiusCardPrimary')};
  --qwenpaw-data-radius-card-secondary: ${px('geometry', 'borderRadiusCardSecondary')};
  --qwenpaw-data-radius-overlay-large: ${px('geometry', 'borderRadiusOverlayLarge')};
  --qwenpaw-data-radius-overlay-small: ${px('geometry', 'borderRadiusOverlaySmall')};
  --qwenpaw-data-control-height-sm: ${px('geometry', 'controlHeightSM')};
  --qwenpaw-data-control-height: ${px('geometry', 'controlHeight')};
  --qwenpaw-data-control-height-lg: ${px('geometry', 'controlHeightLG')};
  --qwenpaw-data-space-xxs: ${px('spacing', 'xxs')};
  --qwenpaw-data-space-xs: ${px('spacing', 'xs')};
  --qwenpaw-data-space-sm: ${px('spacing', 'sm')};
  --qwenpaw-data-space-md: ${px('spacing', 'md')};
  --qwenpaw-data-space-lg: ${px('spacing', 'lg')};
  --qwenpaw-data-space-xl: ${px('spacing', 'xl')};
  --qwenpaw-data-space-xxl: ${px('spacing', 'xxl')};
  --qwenpaw-data-layout-topbar-height: ${px('layout', 'topbarHeight')};
  --qwenpaw-data-layout-sider-width: ${px('layout', 'siderWidth')};
  --qwenpaw-data-layout-route-outer-margin: ${px('layout', 'routeOuterMargin')};
  --qwenpaw-data-layout-route-content-min-width: ${px('layout', 'routeContentMinWidth')};
  --qwenpaw-data-layout-route-content-max-width: ${px('layout', 'routeContentMaxWidth')};
  --qwenpaw-data-density-compact: ${px('density', 'compact')};
  --qwenpaw-data-density-regular: ${px('density', 'regular')};
  --qwenpaw-data-density-spacious: ${px('density', 'spacious')};
  --qwenpaw-data-motion-fast: ${value('motion', 'fast')}ms;
  --qwenpaw-data-motion-collapse: ${value('motion', 'collapse')}ms;
  --qwenpaw-data-motion-layout: ${value('motion', 'layout')}ms;
  --qwenpaw-data-motion-workspace: ${value('motion', 'workspace')}ms;
  --qwenpaw-data-motion-emphasized-easing: ${value('motion', 'emphasizedEasing')};
  --qwenpaw-data-surface-shadow: ${value('elevation', 'surface')};
  --qwenpaw-data-card-hover-shadow: ${value('elevation', 'cardHover')};
  --qwenpaw-data-overlay-shadow: ${value('elevation', 'overlay')};
  --qwenpaw-data-route-card-shadow: ${value('elevation', 'routeCard')};
  --qwenpaw-data-route-card-shadow-large: ${value('elevation', 'routeCardLarge')};
  --qwenpaw-data-ambient-background: ${tokens.ambientBackground.$value};
}

:root[data-qwenpaw-data-theme='dark'] {
${colorVariables('colorDark')}
${surfaceVariables('surfaceDark')}
  --qwenpaw-data-ambient-background: ${tokens.ambientBackgroundDark.$value};
}
`;

if (process.argv.includes('--check')) {
  const current = await readFile(outputPath, 'utf8');
  if (current !== css) {
    console.error('src/tokens.css is stale. Run npm run build in packages/qwenpaw-data-ui-tokens.');
    process.exitCode = 1;
  }
} else {
  await writeFile(outputPath, css);
}
