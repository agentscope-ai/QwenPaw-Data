import type { ArtifactRef } from './contracts';
import { uiRegistry } from './registry';

let preview: { key: string; status: string } | null = null;
export function isArtifactReady(ref: ArtifactRef) {
  return preview?.key === `${ref.session_id}:${ref.artifact_id}` && preview.status === 'ready';
}
export function isArtifactFailed(ref: ArtifactRef) {
  return (
    preview?.key === `${ref.session_id}:${ref.artifact_id}` &&
    ['error', 'unsupported'].includes(preview.status)
  );
}
/** Register only the rendered preview and real headings. Sandbox documents stay opaque. */
export function registerArtifactPreview(ref: ArtifactRef, status: string, root: HTMLElement) {
  const key = `${ref.session_id}:${ref.artifact_id}`;
  preview = { key, status };
  const cleanups: (() => void)[] = [];
  if (status === 'ready') {
    const elements = [
      { id: 'artifact.preview', node: root },
      ...Array.from(root.querySelectorAll<HTMLElement>('h1,h2,h3,h4,h5,h6')).map((node, index) => ({
        id: `artifact.heading:${ref.artifact_id}:${index}`,
        node,
      })),
    ];
    for (const { id, node } of elements) {
      cleanups.push(
        uiRegistry.register(
          { element_id: id, allowed_actions: ['focus', 'scroll'], object_ref: ref },
          async (action, context) => {
            context.assertCurrent();
            if (!node.isConnected || (action.type === 'scroll' && action.anchor_id !== id))
              throw new Error('TARGET_NOT_FOUND');
            node.scrollIntoView({ block: 'start' });
            if (action.type === 'focus') {
              node.tabIndex = -1;
              node.focus();
            }
            return { object_ref: ref, visible: true, completed_steps: [action.type] };
          },
        ),
      );
    }
  }
  uiRegistry.touch();
  return () => {
    cleanups.forEach((fn) => fn());
    if (preview?.key === key) preview = null;
    uiRegistry.touch();
  };
}
