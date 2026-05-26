import type { Series } from '../api/workspaces';

/** Dependency-free area+line chart for the detail page. Normalises the series
 *  to a fixed viewBox (stretches responsively) and labels the peak + time axis. */
export function Chart({
  points,
  color = '#6ea8fe',
  fmt,
}: {
  points: Series;
  color?: string;
  fmt: (n: number) => string;
}) {
  if (points.length < 2) {
    return <div class="chart-empty">No data in this range.</div>;
  }
  const W = 600;
  const H = 130;
  const vals = points.map((p) => p[1]);
  const ts = points.map((p) => p[0]);
  const max = Math.max(...vals);
  const min = Math.min(...vals, 0);
  const range = max - min || 1;
  const stepX = W / (points.length - 1);
  const x = (i: number) => i * stepX;
  const y = (v: number) => H - ((v - min) / range) * H;
  const line = vals.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  const area = `0,${H} ${line} ${W},${H}`;
  const fmtTime = (sec: number) =>
    new Date(sec * 1000).toLocaleString([], {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  return (
    <div class="chart">
      <div class="chart-peak">peak {fmt(max)}</div>
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" class="chart-svg" aria-hidden="true">
        <polygon points={area} fill={color} opacity="0.12" />
        <polyline
          points={line}
          fill="none"
          stroke={color}
          stroke-width="1.5"
          vector-effect="non-scaling-stroke"
        />
      </svg>
      <div class="chart-axis">
        <span>{fmtTime(ts[0])}</span>
        <span>{fmtTime(ts[ts.length - 1])}</span>
      </div>
    </div>
  );
}
