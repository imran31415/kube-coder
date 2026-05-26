import type { Series } from '../api/workspaces';

/** Tiny dependency-free trend line — normalises the series to the viewBox. */
export function Sparkline({
  points,
  color = '#6ea8fe',
  width = 150,
  height = 30,
}: {
  points: Series;
  color?: string;
  width?: number;
  height?: number;
}) {
  const vals = points.map((p) => p[1]);
  if (vals.length < 2) return <div class="spark-empty" />;
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const range = max - min || 1;
  const stepX = width / (vals.length - 1);
  const path = vals
    .map((v, i) => `${(i * stepX).toFixed(1)},${(height - ((v - min) / range) * height).toFixed(1)}`)
    .join(' ');
  return (
    <svg class="spark" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-hidden="true">
      <polyline
        points={path}
        fill="none"
        stroke={color}
        stroke-width="1.5"
        vector-effect="non-scaling-stroke"
      />
    </svg>
  );
}
