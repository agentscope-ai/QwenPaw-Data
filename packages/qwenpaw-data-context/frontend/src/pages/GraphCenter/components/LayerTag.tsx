import type { LayerId } from '../types';
import styles from '../index.module.less';

/** Colored layer pill (MG/TG/KG/UG) — mirrors the demo's .tag-layer. */
export default function LayerTag({ layer }: { layer: LayerId }) {
  return <span className={`${styles.layerTag} ${styles[`layerTag${layer}`]}`}>{layer}</span>;
}
