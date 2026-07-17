import { useEffect, useState } from 'preact/hooks';
import { signal } from '@preact/signals';
import {
  metrics,
  health,
  metricsError,
  metricsLastFetch,
  refreshMetrics,
  startMetricsPolling,
} from '../../store/metrics';
import {
  tasks,
  startTaskPolling,
  selectTask,
  killTask,
} from '../../store/tasks';
import type { TaskSummary } from '../../api/tasks';
import { navigate } from '../../store/router';
import { listApps, proxyUrl, type AppEntry } from '../../api/apps';
import { Pill } from '../../components/primitives/Pill';
import { Button } from '../../components/primitives/Button';
import { Icon } from '../../components/Icon';
import { MutatorOnly } from '../../components/MutatorOnly';
import { ConfirmDialog } from '../../components/ConfirmDialog';

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

/** A small info icon that explains a subsection via its tooltip — the issue
 *  (#285) asks for tooltips "so people understand what is running". */
function InfoHint({ text }: { text: string }) {
  return (
    <span class="metrics-info" title={text} aria-label={text} role="img">
      <Icon name="info" size={14} />
    </span>
  );
}

// Locally-listening web apps (dev servers / previews). Fetched alongside the
// metrics poll so the "Ports & services" list reflects what's live right now.
// Module-scoped so the list survives remounts of the Settings route.
const runningApps = signal<AppEntry[]>([]);

async function refreshApps(): Promise<void> {
  try {
    const res = await listApps();
    runningApps.value = res.apps.filter((a) => a.status === 'running');
  } catch {
    // Non-fatal: the ports list is a convenience overlay on the metrics tab.
  }
}

function RunningTasksBlock() {
  const [killId, setKillId] = useState<string | null>(null);
  const live: TaskSummary[] = tasks.value.filter(
    (t) => t.status === 'running' || t.status === 'waiting-for-input',
  );

  function open(id: string) {
    navigate(`/tasks/${id}`);
    selectTask(id);
  }

  return (
    <div class="metrics-running">
      <div class="metrics-running-head">
        <h3 class="metrics-running-title">Running builds &amp; chats</h3>
        <InfoHint text="Live Claude and terminal sessions in this workspace. Open one to watch its output or stop it if it's stuck." />
        <Pill tone={live.length ? 'accent' : 'neutral'} mono>{live.length}</Pill>
      </div>
      {live.length === 0 ? (
        <div class="metrics-empty muted">No builds or chats are running right now.</div>
      ) : (
        <ul class="metrics-list">
          {live.map((t) => {
            const label = t.name || t.prompt || t.task_id;
            const waiting = t.status === 'waiting-for-input';
            return (
              <li key={t.task_id} class="metrics-item">
                <span class="metrics-item-main">
                  <Pill tone={waiting ? 'warn' : 'success'} mono title={waiting ? 'Waiting for your input' : 'Running'}>
                    {waiting ? 'waiting' : 'running'}
                  </Pill>
                  <span class="metrics-item-label" title={label}>{label}</span>
                  <span class="metrics-item-meta mono muted">{t.kind}{t.source ? ` · ${t.source}` : ''}</span>
                </span>
                <span class="metrics-item-actions">
                  <Button size="sm" variant="ghost" onClick={() => open(t.task_id)} title="Open this session in the Build tab">
                    Open
                  </Button>
                  <MutatorOnly>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => setKillId(t.task_id)}
                      title="Stop this session"
                      aria-label="Stop this session"
                    >
                      <Icon name="kill" size={14} />
                    </Button>
                  </MutatorOnly>
                </span>
              </li>
            );
          })}
        </ul>
      )}
      <ConfirmDialog
        open={killId != null}
        title="Stop this session?"
        body="The tmux session and its process are terminated. This can't be undone."
        confirmLabel="Stop"
        destructive
        onConfirm={() => {
          const id = killId;
          setKillId(null);
          if (id) void killTask(id);
        }}
        onCancel={() => setKillId(null)}
      />
    </div>
  );
}

function PortsBlock() {
  const apps = runningApps.value;
  return (
    <div class="metrics-running">
      <div class="metrics-running-head">
        <h3 class="metrics-running-title">Ports &amp; services</h3>
        <InfoHint text="Web servers currently listening inside the pod — dev servers, previews, and app builds. Open one to view it in the Apps tab." />
        <Pill tone={apps.length ? 'accent' : 'neutral'} mono>{apps.length}</Pill>
      </div>
      {apps.length === 0 ? (
        <div class="metrics-empty muted">Nothing is listening on a port right now.</div>
      ) : (
        <ul class="metrics-list">
          {apps.map((a) => {
            const label = a.name || `Port ${a.port}`;
            return (
              <li key={a.port} class="metrics-item">
                <span class="metrics-item-main">
                  <Pill tone="info" mono title={`Listening on ${a.addr}:${a.port}`}>:{a.port}</Pill>
                  <span class="metrics-item-label" title={label}>{label}</span>
                  <span class="metrics-item-meta mono muted">{a.addr}{a.pinned ? ' · pinned' : ''}</span>
                </span>
                <span class="metrics-item-actions">
                  <Button size="sm" variant="ghost" onClick={() => navigate(`/apps/${a.port}`)} title="Open in the Apps tab">
                    Open
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => window.open(proxyUrl(a.port), '_blank', 'noopener')}
                    title="Open in a new browser tab"
                    aria-label="Open in a new browser tab"
                  >
                    <Icon name="link" size={14} />
                  </Button>
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

export function MetricsSection() {
  useEffect(() => {
    startMetricsPolling(10000);
    startTaskPolling(10000);
    void refreshApps();
    const id = window.setInterval(() => {
      if (typeof document !== 'undefined' && document.hidden) return;
      void refreshApps();
    }, 10000);
    return () => window.clearInterval(id);
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
          <Button size="sm" variant="ghost" onClick={() => { void refreshMetrics(); void refreshApps(); }} title="Refresh now">
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

      <RunningTasksBlock />
      <PortsBlock />

      {h && (
        <div class="metrics-running">
          <div class="metrics-running-head">
            <h3 class="metrics-running-title">Workspace services</h3>
            <InfoHint text="Built-in workspace services and the ports they listen on (VS Code, terminal, browser, dashboard)." />
          </div>
          <div class="health-grid" aria-label="Workspace service health">
            {Object.entries(h.services).map(([name, svc]) => (
              <div key={name} class="health-cell" title={`${name} — ${svc.status} on port ${svc.port}`}>
                <Pill tone={svc.status === 'up' ? 'success' : 'danger'} mono>
                  {svc.status}
                </Pill>
                <span class="health-cell-name">{name}</span>
                <span class="health-cell-port mono muted">:{svc.port}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}
