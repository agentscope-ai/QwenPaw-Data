/** Time-series area chart — hand-built SVG, no chart lib.
 *  Violet→teal gradient fill under a thin violet stroke; optional secondary
 *  amber line (e.g. failed traces). Horizontal gridlines at hairline weight,
 *  micro-caps x labels. */
export interface AreaPoint {
  label: string;
  value: number;
}

interface GradientAreaProps {
  points: AreaPoint[];
  /** Overlay series drawn as a thin amber line (no fill). */
  secondary?: AreaPoint[];
  width?: number;
  height?: number;
  primaryColor?: string;
  secondaryColor?: string;
  emptyText?: string;
}

const GRID_COLOR = 'rgba(31, 31, 31, 0.06)';

export default function GradientArea({
  points,
  secondary,
  width = 420,
  height = 128,
  primaryColor = '#7C4DD6',
  secondaryColor = '#FAAD14',
  emptyText,
}: GradientAreaProps) {
  const padL = 6;
  const padR = 6;
  const padT = 8;
  const padB = 18;
  const innerW = width - padL - padR;
  const innerH = height - padT - padB;

  if (!points.length) {
    return (
      <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img">
        <rect
          x={width * 0.2}
          y={height * 0.3}
          width={width * 0.6}
          height={height * 0.4}
          rx={10}
          fill="none"
          stroke="rgba(31,31,31,0.12)"
          strokeWidth={1}
          strokeDasharray="4 5"
        />
        <text
          x={width / 2}
          y={height / 2 + 3}
          textAnchor="middle"
          fill="#8C8C8C"
          style={{ fontSize: 11 }}
        >
          {emptyText ?? ''}
        </text>
      </svg>
    );
  }

  const maxV = Math.max(...points.map((p) => p.value), ...(secondary || []).map((p) => p.value), 1);
  const xAt = (i: number) =>
    points.length === 1 ? padL + innerW / 2 : padL + (innerW * i) / (points.length - 1);
  const yAt = (v: number) => padT + innerH * (1 - v / maxV);

  const linePath = points
    .map((p, i) => `${i === 0 ? 'M' : 'L'} ${xAt(i)} ${yAt(p.value)}`)
    .join(' ');
  const areaPath = `${linePath} L ${xAt(points.length - 1)} ${padT + innerH} L ${xAt(0)} ${padT + innerH} Z`;
  const secPath = (secondary || [])
    .map((p, i) => `${i === 0 ? 'M' : 'L'} ${xAt(i)} ${yAt(p.value)}`)
    .join(' ');

  const labelEvery = Math.max(1, Math.ceil(points.length / 7));

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      style={{ display: 'block' }}
    >
      <defs>
        <linearGradient id="dash-area-fill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={primaryColor} stopOpacity={0.16} />
          <stop offset="100%" stopColor="#14B8A6" stopOpacity={0.02} />
        </linearGradient>
      </defs>

      {[0.25, 0.5, 0.75, 1].map((f) => (
        <line
          key={f}
          x1={padL}
          x2={width - padR}
          y1={padT + innerH * f}
          y2={padT + innerH * f}
          stroke={GRID_COLOR}
          strokeWidth={0.75}
        />
      ))}

      <path d={areaPath} fill="url(#dash-area-fill)" />
      <path
        d={linePath}
        fill="none"
        stroke={primaryColor}
        strokeWidth={1.5}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      {secPath && (
        <path
          d={secPath}
          fill="none"
          stroke={secondaryColor}
          strokeWidth={1}
          strokeLinejoin="round"
          strokeDasharray="3 3"
        />
      )}

      {points.map((p, i) =>
        i % labelEvery === 0 ? (
          <text
            key={p.label + i}
            x={xAt(i)}
            y={height - 4}
            textAnchor="middle"
            fill="#8C8C8C"
            style={{ fontSize: 8.5, letterSpacing: '0.1em', fontWeight: 600 }}
          >
            {p.label}
          </text>
        ) : null,
      )}

      {points.length > 0 && (
        <circle
          cx={xAt(points.length - 1)}
          cy={yAt(points[points.length - 1].value)}
          r={2.5}
          fill="#14B8A6"
        />
      )}
    </svg>
  );
}
