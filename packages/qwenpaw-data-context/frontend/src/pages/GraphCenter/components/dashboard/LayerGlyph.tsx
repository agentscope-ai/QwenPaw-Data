/** Layer glyph — canonical four-layer shapes (design D7):
 *  MG hexagon · TG diamond · KG rounded rect · UG circle, in LAYER_COLOR. */
import type { LayerId } from '../../types';
import { LAYER_COLOR } from '../../dataset/constants';

interface LayerGlyphProps {
  layer: LayerId;
  size?: number;
}

export default function LayerGlyph({ layer, size = 14 }: LayerGlyphProps) {
  const color = LAYER_COLOR[layer];
  const c = size / 2;
  if (layer === 'MG') {
    const pts = Array.from({ length: 6 }, (_, i) => {
      const a = ((60 * i - 30) * Math.PI) / 180;
      return `${c + c * 0.92 * Math.cos(a)},${c + c * 0.92 * Math.sin(a)}`;
    }).join(' ');
    return (
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden>
        <polygon points={pts} fill={color} />
      </svg>
    );
  }
  if (layer === 'TG') {
    return (
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden>
        <rect
          x={size * 0.18}
          y={size * 0.18}
          width={size * 0.64}
          height={size * 0.64}
          fill={color}
          transform={`rotate(45 ${c} ${c})`}
        />
      </svg>
    );
  }
  if (layer === 'KG') {
    return (
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden>
        <rect
          x={size * 0.12}
          y={size * 0.12}
          width={size * 0.76}
          height={size * 0.76}
          rx={size * 0.18}
          fill={color}
        />
      </svg>
    );
  }
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden>
      <circle cx={c} cy={c} r={size * 0.44} fill={color} />
    </svg>
  );
}
