import { useEffect } from 'preact/hooks';
import {
  metrics,
  health,
  metricsError,
  metricsLastFetch,
  refreshMetrics,
  startMetricsPolling,
} from '../../store/metrics';
import { Pill } from '../../components/primitives/Pill';
import { Button } from '../../components/primitives/Button';

function tone(percent: number): 'success' | 'warn' | 'danger' {
  if (percent >= 90) return 'danger';
  if (percent >= 75) return 'warn';
  return 'success';
}

function MeterRow({ label, percent, hint }: { label: string; percent: number; hint: string }) {
  const t = tone(percent);
  return (
    <div class="meter-row">
      <div class="meter-row-head">
        <span class="meter-row-label">{label}</span>
        <span class={`meter-row-pct meter-row-pct-${t} mono`}>{Math.round(percent)}%</span>
      </div>
      <div class={`meter-track meter-track-${t}`} role="progressbar" aria-valuenow={Math.round(percent)} aria-valuemin={0} aria-valuemax={100}>
        <div class={`meter-fill meter-fill-${t}`} style={{ width: `${Math.min(100, Math.max(0, percent))}%` }} />
      </div>
      <div class="meter-row-hint muted mono">{hint}</div>
    </div>
  );
}

export function MetricsSection() {
  useEffect(() => {
    startMetricsPolling(10000);
  }, []);

  const m = metrics.value;
  const h = health.value;
  const err = metricsError.value;
  const lastFetched = metricsLastFetch.value;
  const secsAgo = lastFetched ? Math.max(0, Math.floor((Date.now() - lastFetched) / 1000)) : null;

  return (
    <section class="settings-section">
      <div class="metrics-head">
        <h2 class="settings-section-title">System metrics</h2>
        <div class="metrics-head-actions">
          {secsAgo != null && (
            <span class="muted" style={{ fontSize: 11.5 }}>
              {err ? 'fetch failed' : `updated ${secsAgo}s ago · polls every 10s`}
            </span>
          )}
          <Button size="sm" variant="ghost" onClick={() => void refreshMetrics()} title="Refresh now">
            Refresh
          </Button>
        </div>
      </div>

      {!m && !err && (
        <div class="muted" style={{ padding: 'var(--size-3) 0', fontSize: 13 }}>Loading metrics…</div>
      )}
      {err && !m && (
        <div class="metrics-error" role="alert">{err}</div>
      )}

      {m && (
        <>
          {m.alerts.length > 0 && (
            <div class="metrics-alerts">
              {m.alerts.map((a, i) => (
                <div
                  key={i}
                  class={`metrics-alert metrics-alert-${a.type}`}
                  role={a.type === 'critical' ? 'alert' : 'status'}
                >
                  <strong>{a.type === 'critical' ? 'Critical' : 'Warning'}</strong> — {a.message}
                </div>
              ))}
            </div>
          )}

          <div class="meter-grid">
            <MeterRow
              label="CPU"
              percent={m.cpu.usage_percent}
              hint={`${m.cpu.usage_percent}% of ${m.cpu.cores} core${m.cpu.cores === 1 ? '' : 's'}`}
            />
            <MeterRow
              label="Memory"
              percent={m.memory.percent}
              hint={`${m.memory.used_mb.toFixed(0)} / ${m.memory.total_mb.toFixed(0)} MB used · ${m.memory.available_mb.toFixed(0)} MB free`}
            />
            <MeterRow
              label="Disk"
              percent={m.disk.percent}
              hint={`${m.disk.used_gb.toFixed(1)} / ${m.disk.total_gb.toFixed(1)} GB used · ${m.disk.available_gb.toFixed(1)} GB free (${m.disk.path})`}
            />
          </div>
        </>
      )}

      {h && (
        <div class="health-grid" aria-label="Workspace service health">
          {Object.entries(h.services).map(([name, svc]) => (
            <div key={name} class="health-cell">
              <Pill tone={svc.status === 'up' ? 'success' : 'danger'} mono>
                {svc.status}
              </Pill>
              <span class="health-cell-name">{name}</span>
              <span class="health-cell-port mono muted">:{svc.port}</span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
