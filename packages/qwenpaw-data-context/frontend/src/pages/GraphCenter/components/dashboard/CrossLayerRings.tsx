/** Widget 4 · Cross-layer links — supported layer glyphs on a circle connected by
 *  layer-gradient ribbons, width ∝ edge count (MultiGraphView idiom). */
import { useId, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { LAYER_COLOR } from '../../dataset/constants';
import { LAYERS, type GraphDb } from '../../dataset/graphDb';
import type { LayerId } from '../../types';
import DashCard from './DashCard';
import LayerGlyph from './LayerGlyph';
import styles from '../../dashboard.module.less';

interface Pair {
  a: LayerId;
  b: LayerId;
  count: number;
}

const POS: Record<LayerId, [number, number]> = {
  MG: [50, 12],
  TG: [88, 50],
  KG: [50, 88],
  UG: [12, 50],
};

export default function CrossLayerRings({ db }: { db: GraphDb }) {
  const { t } = useTranslation();
  const uid = useId().replace(/:/g, '');

  const pairs = useMemo<Pair[]>(() => {
    const counts = db.crossLayerCounts('all');
    const out: Pair[] = [];
    Object.entries(counts).forEach(([key, count]) => {
      const [a, b] = key.split('↔') as [LayerId, LayerId];
      if (a && b) out.push({ a, b, count });
    });
    return out.sort((x, y) => y.count - x.count);
  }, [db]);

  const max = Math.max(...pairs.map((p) => p.count), 1);
  const total = pairs.reduce((s, p) => s + p.count, 0);
  const layerShort = (layer: LayerId) =>
    t(`graphCenter.dashboard.layerShort${layer[0]}${layer.slice(1).toLowerCase()}`);

  return (
    <DashCard
      title={t('graphCenter.dashboard.crossTitle')}
      microLabel={t('graphCenter.dashboard.crossMicro')}
    >
      {total === 0 ? (
        <div className={styles.emptyBlock}>
          <span className={styles.dashedRing} />
          {t('graphCenter.dashboard.emptyGeneric')}
        </div>
      ) : (
        <div className={styles.crossWrap}>
          <div className={styles.crossStage}>
            <svg viewBox="0 0 100 100" className={styles.crossSvg} aria-hidden>
              <defs>
                {pairs.map((p, i) => (
                  <linearGradient
                    key={i}
                    id={`xc-${uid}-${i}`}
                    x1={POS[p.a][0]}
                    y1={POS[p.a][1]}
                    x2={POS[p.b][0]}
                    y2={POS[p.b][1]}
                    gradientUnits="userSpaceOnUse"
                  >
                    <stop offset="0%" stopColor={LAYER_COLOR[p.a]} stopOpacity={0.75} />
                    <stop offset="100%" stopColor={LAYER_COLOR[p.b]} stopOpacity={0.75} />
                  </linearGradient>
                ))}
              </defs>
              <circle
                cx={50}
                cy={50}
                r={38}
                fill="none"
                stroke="rgba(31,31,31,0.05)"
                strokeWidth={0.5}
                strokeDasharray="2 3"
              />
              {pairs.map((p, i) => {
                const [x1, y1] = POS[p.a];
                const [x2, y2] = POS[p.b];
                const w = 0.8 + (p.count / max) * 3.2;
                return (
                  <path
                    key={i}
                    className="dash-ribbon"
                    d={`M ${x1} ${y1} Q 50 50 ${x2} ${y2}`}
                    fill="none"
                    stroke={`url(#xc-${uid}-${i})`}
                    strokeWidth={w}
                    strokeLinecap="round"
                    opacity={0.85}
                  />
                );
              })}
            </svg>
            {LAYERS.map((layer) => (
              <div
                key={layer}
                className={styles.crossNode}
                style={{ left: `${POS[layer][0]}%`, top: `${POS[layer][1]}%` }}
              >
                <span className={styles.crossGlyph}>
                  <LayerGlyph layer={layer} size={18} />
                </span>
                <span className={styles.dashMicro}>{layerShort(layer)}</span>
              </div>
            ))}
          </div>
          <ul className={styles.crossList}>
            {pairs.map((p) => (
              <li key={`${p.a}-${p.b}`}>
                <span className={styles.crossPair}>
                  <LayerGlyph layer={p.a} size={10} />
                  <span className={styles.crossPairLabel}>
                    {layerShort(p.a)} ↔ {layerShort(p.b)}
                  </span>
                  <LayerGlyph layer={p.b} size={10} />
                </span>
                <span className={styles.crossCount}>×{p.count}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </DashCard>
  );
}
