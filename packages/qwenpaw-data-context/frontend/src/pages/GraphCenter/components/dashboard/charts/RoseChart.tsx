/** Nightingale rose (polar area) — hand-built SVG, no chart lib.
 *  Equal-angle petals, radius ∝ √(value/max), thin concentric gridlines,
 *  center total in tabular-nums with a micro-caps caption. */
import { useId } from 'react';

export interface RoseItem {
  label: string;
  value: number;
  color: string;
  /** Extra caption line under the label (e.g. "节点 156"). */
  caption?: string;
}

interface RoseChartProps {
  items: RoseItem[];
  size?: number;
  centerValue?: string | number;
  centerLabel?: string;
}

const GRID_COLOR = 'rgba(31, 31, 31, 0.06)';

function polar(cx: number, cy: number, r: number, angleDeg: number): [number, number] {
  const rad = ((angleDeg - 90) * Math.PI) / 180;
  return [cx + r * Math.cos(rad), cy + r * Math.sin(rad)];
}

function petalPath(cx: number, cy: number, r: number, a0: number, a1: number): string {
  const [x0, y0] = polar(cx, cy, r, a0);
  const [x1, y1] = polar(cx, cy, r, a1);
  const large = a1 - a0 > 180 ? 1 : 0;
  return `M ${cx} ${cy} L ${x0} ${y0} A ${r} ${r} 0 ${large} 1 ${x1} ${y1} Z`;
}

export default function RoseChart({ items, size = 220, centerValue, centerLabel }: RoseChartProps) {
  const uid = useId().replace(/:/g, '');
  const cx = size / 2;
  const cy = size / 2;
  const maxR = size / 2 - 14;
  const innerR = size * 0.16;
  const max = Math.max(...items.map((i) => i.value), 1);
  const step = 360 / Math.max(items.length, 1);

  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      role="img"
      style={{ display: 'block', overflow: 'visible' }}
    >
      <defs>
        {items.map((item, i) => (
          <radialGradient key={i} id={`rose-g-${uid}-${i}`} cx="50%" cy="50%" r="65%">
            <stop offset="0%" stopColor={item.color} stopOpacity={0.35} />
            <stop offset="100%" stopColor={item.color} stopOpacity={0.8} />
          </radialGradient>
        ))}
      </defs>

      {[0.33, 0.66, 1].map((f) => (
        <circle
          key={f}
          cx={cx}
          cy={cy}
          r={maxR * f}
          fill="none"
          stroke={GRID_COLOR}
          strokeWidth={0.75}
        />
      ))}

      {items.map((item, i) => {
        const r = innerR + (maxR - innerR) * Math.sqrt(item.value / max);
        const a0 = i * step + 1.2;
        const a1 = (i + 1) * step - 1.2;
        const [lx, ly] = polar(cx, cy, maxR + 2, (a0 + a1) / 2);
        return (
          <g key={item.label} className="dash-rose-petal">
            <path
              d={petalPath(cx, cy, r, a0, a1)}
              fill={`url(#rose-g-${uid}-${i})`}
              stroke={item.color}
              strokeWidth={0.75}
            />
            <text
              x={lx}
              y={ly}
              textAnchor="middle"
              dominantBaseline="middle"
              fill="#8C8C8C"
              style={{ fontSize: 9, letterSpacing: '0.04em', fontWeight: 600 }}
            >
              {item.label}
            </text>
          </g>
        );
      })}

      <circle cx={cx} cy={cy} r={innerR - 3} fill="#fff" stroke={GRID_COLOR} strokeWidth={0.75} />
      <text
        x={cx}
        y={cy - 2}
        textAnchor="middle"
        fill="#1F1F1F"
        style={{ fontSize: 17, fontWeight: 600, fontVariantNumeric: 'tabular-nums' }}
      >
        {centerValue ?? ''}
      </text>
      <text
        x={cx}
        y={cy + 12}
        textAnchor="middle"
        fill="#8C8C8C"
        style={{ fontSize: 8, letterSpacing: '0.04em', fontWeight: 600 }}
      >
        {centerLabel ?? ''}
      </text>
    </svg>
  );
}
