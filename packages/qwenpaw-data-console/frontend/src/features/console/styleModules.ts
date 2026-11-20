type StyleModule = Readonly<Record<string, string>>;

/**
 * Combines owner and shared CSS Module class names without coupling component
 * files back to the feature-level stylesheet.
 */
export function mergeStyleModules(
  ownerStyles: StyleModule,
  sharedStyles: StyleModule,
): Record<string, string> {
  const mergedStyles: Record<string, string> = { ...sharedStyles };

  for (const [name, className] of Object.entries(ownerStyles)) {
    mergedStyles[name] = mergedStyles[name] ? `${className} ${mergedStyles[name]}` : className;
  }

  return mergedStyles;
}
