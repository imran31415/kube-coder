import { type ResourceRollup } from '../api/capacity';
import { fmtPct, tone } from '../format';

/** A horizontal capacity bar: a workspace segment + an "other tenants" segment
 *  stacked against allocatable, with the remainder as free headroom. The bar is
 *  toned by total utilisation so a nearly-full node reads red at a glance.
 *
 *  When allocatable is unknown (kube-state-metrics absent), the percentage math
 *  is undefined — we render a flat "capacity unknown" track instead of a
 *  misleading full/empty bar. */
export function CapacityBar({
  rollup,
  fmt,
  warn = 75,
  crit = 90,
}: {
  rollup: ResourceRollup;
  fmt: (n: number) => string;
  warn?: number;
  crit?: number;
}) {
  const { allocatable, workspace, other, workspacePct, clusterPct } = rollup;
  if (allocatable == null || allocatable <= 0) {
    return (
      <div class="capbar unknown" title="No allocatable capacity reported by kube-state-metrics">
        <div class="capbar-track" />
        <div class="capbar-legend">
          <span>{fmt(workspace)} workspaces</span>
          <span class="muted">capacity unknown</span>
        </div>
      </div>
    );
  }
  const wsPct = Math.max(0, Math.min(100, workspacePct ?? 0));
  // `other` is clamped >= 0 by the backend; cap the visual at 100% so scrape
  // skew can't overflow the track.
  const otherPct = Math.max(0, Math.min(100 - wsPct, ((other ?? 0) / allocatable) * 100));
  const t = tone(clusterPct, warn, crit);
  const headroom = Math.max(0, allocatable - (workspace + (other ?? 0)));
  return (
    <div class={`capbar t-${t}`}>
      <div class="capbar-track" role="img" aria-label={`${fmtPct(clusterPct)} used`}>
        <div class="capbar-seg ws" style={{ width: `${wsPct}%` }} title={`workspaces ${fmt(workspace)}`} />
        <div class="capbar-seg other" style={{ width: `${otherPct}%` }} title={`other tenants ${fmt(other ?? 0)}`} />
      </div>
      <div class="capbar-legend">
        <span><i class="dot ws" /> {fmt(workspace)} ws ({fmtPct(workspacePct)})</span>
        <span><i class="dot other" /> {fmt(other ?? 0)} other</span>
        <span class="capbar-head">{fmt(headroom)} free of {fmt(allocatable)}</span>
      </div>
    </div>
  );
}
