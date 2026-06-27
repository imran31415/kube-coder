import { useEffect } from 'preact/hooks';
import { signal } from '@preact/signals';
import { type Workspace, type WorkspaceState } from './api/workspaces';
import {
  workspaces,
  namespace,
  loaded,
  error,
  busy,
  refresh,
  toggle,
  provisionConfig,
  loadProvisionConfig,
} from './store';
import { route, detailUser, isProvisionRoute, navigate } from './router';
import { MetricsPanel } from './components/MetricsPanel';
import { WorkspaceDetail } from './components/WorkspaceDetail';
import { InsightsBar } from './components/InsightsBar';
import { CapacityPanel } from './components/CapacityPanel';
import { ProvisionForm } from './components/ProvisionForm';

// The single workspace whose inline metrics panel is expanded (null = collapsed).
const expanded = signal<string | null>(null);

export function App() {
  // The 5s list poll runs for the whole app lifetime (both views need state).
  useEffect(() => {
    void refresh();
    void loadProvisionConfig();
    const id = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(id);
  }, []);

  const path = route.value;
  if (isProvisionRoute(path)) return <ProvisionForm />;
  const user = detailUser(path);
  return user ? <WorkspaceDetail user={user} /> : <WorkspaceList />;
}

function WorkspaceList() {
  const rows = workspaces.value;
  return (
    <div class="app">
      <header class="hdr">
        <div>
          <h1>Workspaces</h1>
          <p class="sub">
            {namespace.value ? `namespace ${namespace.value} · ` : ''}
            Start or stop a workspace, or open one for detailed usage metrics.
          </p>
        </div>
        <div class="hdr-actions">
          {provisionConfig.value?.enabled && (
            <button class="btn start" onClick={() => navigate('/provision')}>
              + New workspace
            </button>
          )}
          <button class="btn ghost" onClick={() => void refresh()}>
            Refresh
          </button>
        </div>
      </header>

      <InsightsBar />

      <CapacityPanel />

      {error.value && (
        <div class="banner err" role="alert">
          {error.value}
        </div>
      )}

      {loaded.value && rows.length === 0 && !error.value ? (
        <div class="empty">No workspaces found in this namespace.</div>
      ) : (
        <ul class="list" aria-label="Workspaces">
          {rows.map((w) => (
            <Row key={w.deployment} ws={w} />
          ))}
        </ul>
      )}
    </div>
  );
}

function Row({ ws }: { ws: Workspace }) {
  const isBusy = busy.value.has(ws.user);
  const stopped = ws.state === 'stopped';
  const pending = ws.state === 'transitioning' || isBusy;
  const isOpen = expanded.value === ws.user;
  const toggleOpen = () => {
    expanded.value = isOpen ? null : ws.user;
  };
  return (
    <li class={`row-wrap state-${ws.state} ${isOpen ? 'open' : ''}`}>
      <div class="row" onClick={toggleOpen} role="button" aria-expanded={isOpen}>
        <span class={`chev ${isOpen ? 'open' : ''}`} aria-hidden="true">
          ▸
        </span>
        <div class="row-main">
          <div class="row-name">
            {ws.url ? (
              <a href={ws.url} target="_blank" rel="noopener" onClick={(e) => e.stopPropagation()}>
                {ws.user}
              </a>
            ) : (
              ws.user
            )}
            <Pill state={ws.state} />
            {ws.updateAvailable && (
              <span class="pill pill-update" title={`Update available: ${ws.version} → latest`}>
                update
              </span>
            )}
          </div>
          <div class="row-meta">
            {ws.deployment} · {ws.detail}
            {ws.version ? ` · ${ws.version}` : ''}
          </div>
        </div>
        <button
          class={`btn ${stopped ? 'start' : 'stop'}`}
          disabled={pending}
          onClick={(e) => {
            e.stopPropagation();
            void toggle(ws);
          }}
          title={stopped ? 'Start workspace' : 'Stop workspace (data preserved)'}
        >
          {pending ? '…' : stopped ? 'Start' : 'Stop'}
        </button>
      </div>
      {isOpen && <MetricsPanel user={ws.user} />}
    </li>
  );
}

function Pill({ state }: { state: WorkspaceState }) {
  return <span class={`pill pill-${state}`}>{state}</span>;
}
