import type { Series } from '../api/workspaces';

/** Headroom-over-time chart. Cluster usage is the filled area, workspace usage
 *  the solid line, and allocatable a dashed ceiling — so a previous spike reads
 *  as the area climbing toward the dashed line. Dependency-free SVG, normalised
 *  to a fixed viewBox that stretches responsively (mirrors Chart.tsx). */
export function CapacityChart({
  workspace,
  cluster,
  allocatable,
  fmt,
}: {
  workspace: Series;
  cluster: Series;
  allocatable: Series;
  fmt: (n: number) => string;
}) {
  if (cluster.length < 2) {
    return <div class="chart-empty">No data in this range.</div>;
  }
  const W = 600;
  const H = 130;
  const all = [...workspace, ...cluster, ...allocatable].map((p) => p[1]);
  const max = Math.max(...all, 0) || 1;
  const ts = cluster.map((p) => p[0]);
  const n = cluster.length;
  const x = (i: number) => (i / (n - 1)) * W;
  const y = (v: number) => H - (v / max) * H;

  const poly = (s: Series) => s.map((p, i) => `${x(i).toFixed(1)},${y(p[1]).toFixed(1)}`).join(' ');
  const clusterLine = poly(cluster);
  const area = `0,${H} ${clusterLine} ${W},${H}`;
  // The ceiling is effectively flat; draw it from its last (current) value so a
  // mid-window node add/removal doesn't produce a misleading sloped line.
  const ceil = allocatable.length ? allocatable[allocatable.length - 1][1] : null;
  const ceilY = ceil != null ? y(ceil) : null;

  const fmtTime = (sec: number) =>
    new Date(sec * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });

  return (
    <div class="chart">
      <div class="chart-peak">
        peak total {fmt(Math.max(...cluster.map((p) => p[1])))}
        {ceil != null && <> · ceiling {fmt(ceil)}</>}
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" class="chart-svg" aria-hidden="true">
        <polygon points={area} fill="#6ea8fe" opacity="0.12" />
        <polyline points={clusterLine} fill="none" stroke="#6ea8fe" stroke-width="1.5" vector-effect="non-scaling-stroke" />
        {workspace.length >= 2 && (
          <polyline points={poly(workspace)} fill="none" stroke="#56d364" stroke-width="1.5" vector-effect="non-scaling-stroke" />
        )}
        {ceilY != null && (
          <line
            x1="0"
            x2={W}
            y1={ceilY.toFixed(1)}
            y2={ceilY.toFixed(1)}
            stroke="#f85149"
            stroke-width="1"
            stroke-dasharray="4 3"
            vector-effect="non-scaling-stroke"
          />
        )}
      </svg>
      <div class="chart-legend">
        <span><i class="dot other" /> total usage</span>
        <span><i class="dot ws" /> workspaces</span>
        <span><i class="dot ceil" /> allocatable</span>
      </div>
      <div class="chart-axis">
        <span>{fmtTime(ts[0])}</span>
        <span>{fmtTime(ts[ts.length - 1])}</span>
      </div>
    </div>
  );
}
