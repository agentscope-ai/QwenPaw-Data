/** Concentric glowing ring gauge — hand-built SVG, no chart lib.
 *  Each ring is a 1.5px stroke that draws in via CSS stroke-dashoffset
 *  animation; a soft drop-shadow gives the reference-image "brand euphoria"
 *  glow. Used for data-health scoring. */
export interface RingSpec {
  /** Fraction 0..1 of the circumference filled. */
  fraction: number;
  color: string;
  /** Optional glow color; defaults to the ring color. */
  glow?: string;
  /** Dashed placeholder look for empty/unknown values. */
  hollow?: boolean;
}

interface RadialGaugeProps {
  rings: RingSpec[];
  size?: number;
  centerValue?: string | number;
  centerLabel?: string;
}

export default function RadialGauge({
  rings,
  size = 168,
  centerValue,
  centerLabel,
}: RadialGaugeProps) {
  const cx = size / 2;
  const cy = size / 2;
  const outerR = size / 2 - 6;
  const gap = Math.min(16, (outerR - size * 0.18) / Math.max(rings.length, 1));

  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      role="img"
      style={{ display: 'block', overflow: 'visible' }}
    >
      {rings.map((ring, i) => {
        const r = outerR - i * gap;
        const c = 2 * Math.PI * r;
        const filled = Math.max(0, Math.min(1, ring.fraction));
        return (
          <g key={i}>
            <circle
              cx={cx}
              cy={cy}
              r={r}
              fill="none"
              stroke="rgba(31,31,31,0.06)"
              strokeWidth={1.5}
            />
            <circle
              className="dash-ring-draw"
              cx={cx}
              cy={cy}
              r={r}
              fill="none"
              stroke={ring.color}
              strokeWidth={ring.hollow ? 1 : 1.5}
              strokeLinecap="round"
              strokeDasharray={ring.hollow ? '3 6' : `${c * filled} ${c}`}
              style={{
                filter:
                  filled > 0 ? `drop-shadow(0 0 5px ${ring.glow || ring.color}59)` : undefined,
                ['--dash-ring-c' as string]: `${c}`,
                ['--dash-ring-fill' as string]: `${c * filled}`,
              }}
              transform={`rotate(-90 ${cx} ${cy})`}
            />
          </g>
        );
      })}
      {centerValue !== undefined && (
        <>
          <text
            x={cx}
            y={cy - 2}
            textAnchor="middle"
            fill="#1F1F1F"
            style={{ fontSize: 20, fontWeight: 600, fontVariantNumeric: 'tabular-nums' }}
          >
            {centerValue}
          </text>
          <text
            x={cx}
            y={cy + 14}
            textAnchor="middle"
            fill="#8C8C8C"
            style={{ fontSize: 9, letterSpacing: '0.04em', fontWeight: 600 }}
          >
            {centerLabel ?? ''}
          </text>
        </>
      )}
    </svg>
  );
}
