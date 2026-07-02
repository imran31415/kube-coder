import { useEffect, useState } from 'preact/hooks';
import { type ClusterHealthResponse, type HealthStatus, getCapacitySummary } from '../api/capacity';
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
 *  so it never flickers. The whole card links through to the full drill-down. */
export function HealthSummary() {
  const [h, setH] = useState<ClusterHealthResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    async function load() {
      try {
        const r = await getCapacitySummary();
        if (!alive) return;
        setH(r);
        setErr(null);
      } catch (e) {
        if (alive && !h) setErr(e instanceof Error ? e.message : String(e));
      }
    }
    void load();
    const id = window.setInterval(() => void load(), 30000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const status: HealthStatus = h?.metricsError ? 'unknown' : (h?.status ?? 'unknown');
  const c = h?.cluster ?? null;
  const open = () => navigate('/capacity');

  return (
    <button class={`health health-${status}`} onClick={open} title="Open cluster resources">
      <span class="health-main">
        <span class={`health-dot dot-${status}`} aria-hidden="true" />
        <span class="health-label">Cluster: {LABEL[status]}</span>
      </span>
      <span class="health-stats">
        {!h && !err && <span class="health-stat muted">Loading…</span>}
        {err && !h && <span class="health-stat muted">unavailable</span>}
        {c && (
          <>
            <span class="health-stat">CPU {fmtPct(c.cpu.clusterPct)}</span>
            <span class="health-stat">Mem {fmtPct(c.memory.clusterPct)}</span>
            <span class="health-stat muted">
              {c.nodeCount} node{c.nodeCount === 1 ? '' : 's'}
            </span>
          </>
        )}
        <span class="health-cta">Cluster resources →</span>
      </span>
    </button>
  );
}
