import { useEffect, useState } from 'preact/hooks';
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
import { route, detailUser, isProvisionRoute, isCapacityRoute, navigate } from './router';
import { MetricsPanel } from './components/MetricsPanel';
import { WorkspaceDetail } from './components/WorkspaceDetail';
import { InsightsBar } from './components/InsightsBar';
import { CapacityPanel } from './components/CapacityPanel';
import { HealthSummary } from './components/HealthSummary';
import { ProvisionForm } from './components/ProvisionForm';
import { MobileAccessCard } from './components/MobileAccessCard';

// The single workspace whose inline metrics panel is expanded (null = collapsed).
const expanded = signal<string | null>(null);

export function App() {
  // The list poll runs for the whole app lifetime (both views need state).
  // 10s is plenty for a status console; refresh() is single-flight so polls
  // never stack, and mutating actions invalidate the server cache for an
  // immediate update rather than relying on a tight interval.
  useEffect(() => {
    void refresh();
    void loadProvisionConfig();
    const id = window.setInterval(() => void refresh(), 10000);
    return () => window.clearInterval(id);
  }, []);

  const path = route.value;
  if (isProvisionRoute(path)) return <ProvisionForm />;
  if (isCapacityRoute(path)) return <CapacityView />;
  const user = detailUser(path);
  return user ? <WorkspaceDetail user={user} /> : <WorkspaceList />;
}

// The cluster-resources drill-down: the full capacity panel (per-node + range
// history) and the insights advisories. These run the heavy Prometheus queries,
// so they live here — reached from the summary page's health card — instead of
// firing on every dashboard load.
function CapacityView() {
  return (
    <div class="app">
      <header class="hdr">
        <div>
          <button class="crumb" onClick={() => navigate('/')}>← Workspaces</button>
          <h1>Cluster resources</h1>
          <p class="sub">
            Live capacity across all nodes, usage history, and per-workspace advisories.
          </p>
        </div>
      </header>
      <InsightsBar />
      <CapacityPanel />
    </div>
  );
}

const PAGE_SIZE = 10;
type StateFilter = 'all' | 'running' | 'stopped';
type NsFilter = 'all' | 'isolated' | 'shared';

function WorkspaceList() {
  const rows = workspaces.value;
  const [query, setQuery] = useState('');
  const [stateF, setStateF] = useState<StateFilter>('all');
  const [nsF, setNsF] = useState<NsFilter>('all');
  const [page, setPage] = useState(0);

  const q = query.trim().toLowerCase();
  const filtered = rows.filter((w) => {
    // "running" groups every active state (running/transitioning/degraded) —
    // the useful split for an operator is active-vs-stopped, not the exact pill.
    if (stateF === 'running' && w.state === 'stopped') return false;
    if (stateF === 'stopped' && w.state !== 'stopped') return false;
    if (nsF === 'isolated' && !w.isolated) return false;
    if (nsF === 'shared' && w.isolated) return false;
    if (!q) return true;
    return (
      w.user.toLowerCase().includes(q) ||
      w.namespace.toLowerCase().includes(q) ||
      (w.version ?? '').toLowerCase().includes(q)
    );
  });

  // Clamp the page against the current filtered length so shrinking the result
  // set (typing, or a workspace disappearing on poll) can't strand an empty page.
  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const cur = Math.min(page, pageCount - 1);
  const start = cur * PAGE_SIZE;
  const pageRows = filtered.slice(start, start + PAGE_SIZE);
  // Any filter change resets to the first page so results are visible.
  const withReset = <T,>(setter: (v: T) => void, v: T) => {
    setter(v);
    setPage(0);
  };

  const stateChips: StateFilter[] = ['all', 'running', 'stopped'];
  const nsChips: NsFilter[] = ['all', 'isolated', 'shared'];

  return (
    <div class="app">
      <header class="hdr">
        <div>
          <h1>Workspaces</h1>
          <p class="sub">
            {namespace.value ? `control plane: ${namespace.value} · ` : ''}
            Each workspace runs in its own namespace. Start or stop one, or open it for detailed usage metrics.
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

      <HealthSummary />

      {error.value && (
        <div class="banner err" role="alert">
          {error.value}
        </div>
      )}

      <div class="filters">
        <input
          class="search"
          type="search"
          placeholder="Search user, namespace, or version…"
          aria-label="Search workspaces"
          value={query}
          onInput={(e) => withReset(setQuery, (e.target as HTMLInputElement).value)}
        />
        <div class="filter-group" role="group" aria-label="Filter by state">
          {stateChips.map((s) => (
            <button key={s} class={`chip ${stateF === s ? 'on' : ''}`} onClick={() => withReset(setStateF, s)}>
              {s === 'all' ? 'All' : s[0].toUpperCase() + s.slice(1)}
            </button>
          ))}
        </div>
        <div class="filter-group" role="group" aria-label="Filter by namespace isolation">
          {nsChips.map((s) => (
            <button key={s} class={`chip ${nsF === s ? 'on' : ''}`} onClick={() => withReset(setNsF, s)}>
              {s === 'all' ? 'Any ns' : s[0].toUpperCase() + s.slice(1)}
            </button>
          ))}
        </div>
      </div>

      {!loaded.value && rows.length === 0 && !error.value ? (
        <div class="empty loading">
          <span class="spinner" aria-hidden="true" />
          Loading workspaces…
        </div>
      ) : loaded.value && rows.length === 0 && !error.value ? (
        <div class="empty">No workspaces found.</div>
      ) : filtered.length === 0 ? (
        <div class="empty">No workspaces match your search or filters.</div>
      ) : (
        <>
          <ul class="list" aria-label="Workspaces">
            {pageRows.map((w) => (
              <Row key={w.deployment} ws={w} />
            ))}
          </ul>
          <div class="pager">
            <span class="pager-info">
              {start + 1}–{Math.min(start + PAGE_SIZE, filtered.length)} of {filtered.length}
              {filtered.length !== rows.length ? ` (filtered from ${rows.length})` : ''}
            </span>
            {filtered.length > PAGE_SIZE && (
              <div class="pager-btns">
                <button class="btn ghost" disabled={cur === 0} onClick={() => setPage(cur - 1)}>
                  ← Prev
                </button>
                <span class="pager-pos">
                  Page {cur + 1} / {pageCount}
                </span>
                <button class="btn ghost" disabled={cur >= pageCount - 1} onClick={() => setPage(cur + 1)}>
                  Next →
                </button>
              </div>
            )}
          </div>
        </>
      )}

      <MobileAccessCard />
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
            <IsolationPill isolated={ws.isolated} namespace={ws.namespace} />
            {ws.updateAvailable && (
              <span class="pill pill-update" title={`Update available: ${ws.version} → latest`}>
                update
              </span>
            )}
          </div>
          <div class="row-meta">
            {ws.namespace} · {ws.detail}
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

// Distinguishes a workspace migrated to its own namespace (#103) from one still
// in the shared control-plane namespace — so a migrated workspace and the
// scaled-to-0 copy left behind in `coder` don't read as accidental duplicates.
function IsolationPill({ isolated, namespace }: { isolated: boolean; namespace: string }) {
  return isolated ? (
    <span class="pill pill-isolated" title={`Isolated in its own namespace (${namespace})`}>
      isolated
    </span>
  ) : (
    <span
      class="pill pill-shared"
      title={`Still in the shared control-plane namespace (${namespace}) — not yet migrated, or a leftover copy of a migrated workspace`}
    >
      shared
    </span>
  );
}
