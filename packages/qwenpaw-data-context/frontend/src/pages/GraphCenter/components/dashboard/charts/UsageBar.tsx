/** Horizontal usage/degree bar — shared by HotspotList and CuratedAssets.
 *  Violet→teal gradient fill; zero-use renders a hairline dashed stub;
 *  high-use rows may enable a soft teal glow. */
interface UsageBarProps {
  value: number;
  max: number;
  /** Show the faint "未使用" stub instead of hiding the bar entirely. */
  zeroStub?: boolean;
  glow?: boolean;
  width?: number | string;
  height?: number;
}

export default function UsageBar({
  value,
  max,
  zeroStub = false,
  glow = false,
  width = '100%',
  height = 4,
}: UsageBarProps) {
  const fraction = max > 0 ? Math.min(1, value / max) : 0;

  if (value <= 0 && zeroStub) {
    return (
      <div
        style={{
          width,
          height,
          borderRadius: height / 2,
          borderTop: '1px dashed rgba(31,31,31,0.15)',
        }}
        aria-hidden
      />
    );
  }

  return (
    <div
      style={{
        width,
        height,
        borderRadius: height / 2,
        background: 'rgba(31,31,31,0.05)',
        overflow: 'hidden',
      }}
      aria-hidden
    >
      <div
        className={glow ? 'dash-bar-glow' : undefined}
        style={{
          width: `${Math.max(fraction * 100, value > 0 ? 4 : 0)}%`,
          height: '100%',
          borderRadius: height / 2,
          background: 'linear-gradient(90deg, #7C4DD6 0%, #14B8A6 100%)',
          transition: 'width 0.5s ease-out',
        }}
      />
    </div>
  );
}
