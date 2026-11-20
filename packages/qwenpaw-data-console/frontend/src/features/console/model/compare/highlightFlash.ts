// Missing start/end keyframes use the existing highlight styles and restore them.
export const DIFF_FLASH_KEYFRAMES: Keyframe[] = [
  {
    offset: 0.5,
    backgroundColor: '#ffc53d',
    boxShadow: '0 0 0 3px rgba(250, 173, 20, 0.5)',
  },
];

export const DIFF_FLASH_OPTIONS: KeyframeAnimationOptions = {
  id: 'qwenpaw-data-diff-flash',
  duration: 600,
  iterations: 3,
  easing: 'ease-in-out',
};

export function flashDiffHighlight(element: Element): void {
  element
    .getAnimations()
    .filter((animation) => animation.id === DIFF_FLASH_OPTIONS.id)
    .forEach((animation) => animation.cancel());
  element.animate(DIFF_FLASH_KEYFRAMES, DIFF_FLASH_OPTIONS);
}
