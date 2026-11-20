export function canUseConsoleLiquidGlass({
  supportsBackdropFilter,
  userAgent,
}: {
  supportsBackdropFilter: boolean;
  userAgent: string;
}): boolean {
  if (!supportsBackdropFilter) return false;

  // @liquidglass/react relies on an SVG url() displacement map inside
  // backdrop-filter. WebKit parses that syntax but does not render the filter,
  // so CSS.supports alone cannot identify Safari's missing capability.
  const isWebKit =
    /AppleWebKit/i.test(userAgent) && !/(?:Chrome|Chromium|Edg|OPR)\//i.test(userAgent);
  return !isWebKit;
}

export function supportsConsoleLiquidGlass(): boolean {
  if (typeof CSS === 'undefined' || typeof navigator === 'undefined') return false;
  return canUseConsoleLiquidGlass({
    supportsBackdropFilter:
      CSS.supports('backdrop-filter', 'blur(1px)') ||
      CSS.supports('-webkit-backdrop-filter', 'blur(1px)'),
    userAgent: navigator.userAgent,
  });
}
