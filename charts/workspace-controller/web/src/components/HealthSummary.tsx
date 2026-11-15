import { usePollingResource } from '../hooks/usePollingResource';
import { ResourceStatus } from './ResourceStatus';
import { type HealthStatus, getCapacitySummary } from '../api/capacity';
import { navigate } from '../router';
import { fmtPct } from '../format';

const LABEL: Record<HealthStatus, string> = {
  ok: 'Healthy',
  warn: 'Under pressure',
  crit: 'Near capacity',
  unknown: 'Metrics unavailable',
};

/** Lightweight cluster-health card for the landing page. Backed by the cheap
 *  /api/capacity/summary (instant queries only), so it can poll without the
 *  heavy range-history/per-node/insights load that the full capacity view runs.
 *  Polls every 30s and keeps the last good reading through a transient failure
 *  so it never flickers. A separate button opens the full drill-down. */
export function HealthSummary() {
  const resource = usePollingResource('health', signal => getCapacitySummary(signal), 30000);
  const h = resource.data;
  const err = resource.problem;
  const status: HealthStatus = resource.problem ? 'unknown' : (h?.status ?? 'unknown');
  const c = h?.cluster ?? null;
  const open = () => navigate('/capacity');

  return (
    <section class={`health health-${status}`} aria-label="Cluster health">
      <span class="health-main">
        <span class={`health-dot dot-${status}`} aria-hidden="true" />
        <span class="health-label">Cluster: {LABEL[status]}</span>
      </span>
      <span class="health-stats">
        {!h && !err && <span class="health-stat muted">Loading…</span>}
        {err && !h && <span class="health-stat muted">unavailable</span>}
        {c && (!resource.problem || resource.updatedAt) && (
          <>
            <span class="health-stat">CPU {fmtPct(c.cpu.clusterPct)}</span>
            <span class="health-stat">Mem {fmtPct(c.memory.clusterPct)}</span>
            <span class="health-stat muted">
              {c.nodeCount} node{c.nodeCount === 1 ? '' : 's'}
            </span>
          </>
        )}
        <button type="button" class="health-cta chip" onClick={open}>Cluster resources</button>
      </span>
      <ResourceStatus {...resource} />
    </section>
  );
}
