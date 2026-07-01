import { useEffect, useState } from 'preact/hooks';
import {
  type Series,
  type Workspace,
  type WorkspaceMetrics,
  getWorkspaceMetrics,
  setWorkspaceResources,
  updateWorkspace,
} from '../api/workspaces';
import { findWorkspace, toggle, busy, latestVersion } from '../store';
import { navigate } from '../router';
import { Chart } from './Chart';
import { fmtBytes, fmtCores, fmtPct, fmtRate, fmtUptime, fmtUsd, tone } from '../format';

/** Bytes → a k8s memory quantity for prefilling the editor (e.g. 4Gi, 512Mi). */
function toMemQty(bytes: number | null): string {
  if (!bytes || bytes <= 0) return '';
  const gi = bytes / 1024 ** 3;
  if (gi >= 1) return `${Number.isInteger(gi) ? gi : gi.toFixed(1)}Gi`;
  return `${Math.round(bytes / 1024 ** 2)}Mi`;
}

/** Cores → a k8s CPU quantity (e.g. 2, or 500m for sub-core). */
function toCpuQty(cores: number | null): string {
  if (cores == null || cores <= 0) return '';
  return Number.isInteger(cores) ? String(cores) : `${Math.round(cores * 1000)}m`;
}

const RANGES = [
  { label: '1h', seconds: 3600 },
  { label: '6h', seconds: 21600 },
  { label: '24h', seconds: 86400 },
  { label: '7d', seconds: 604800 },
];

export function WorkspaceDetail({ user }: { user: string }) {
  const [rangeSec, setRangeSec] = useState(21600);
  const [m, setM] = useState<WorkspaceMetrics | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // Bumped after a resource edit to force an immediate metrics refetch.
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    async function load() {
      try {
        const res = await getWorkspaceMetrics(user, rangeSec);
        if (alive) {
          setM(res);
          setErr(null);
        }
      } catch (e) {
        if (alive) setErr(e instanceof Error ? e.message : String(e));
      } finally {
        if (alive) setLoading(false);
      }
    }
    void load();
    const id = window.setInterval(() => void load(), 15000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [user, rangeSec, reloadKey]);

  const ws = findWorkspace(user);
  const isBusy = busy.value.has(user);
  const running = m?.running ?? ws?.state !== 'stopped';

  return (
    <div class="app">
      <header class="hdr">
        <div>
          <a
            class="back"
            href="#/"
            onClick={(e) => {
              e.preventDefault();
              navigate('/');
            }}
          >
            ← all workspaces
          </a>
          <h1>{user}</h1>
          <p class="sub">
            {ws?.namespace ?? m?.namespace ?? `ws-${user}`}
            {m ? ` · ${running ? 'running' : 'stopped'}` : ''}
            {m?.uptimeSeconds != null ? ` · up ${fmtUptime(m.uptimeSeconds)}` : ''}
          </p>
        </div>
        {ws && (
          <button
            class={`btn ${ws.state === 'stopped' ? 'start' : 'stop'}`}
            disabled={ws.state === 'transitioning' || isBusy}
            onClick={() => void toggle(ws)}
          >
            {ws.state === 'stopped' ? 'Start' : 'Stop'}
          </button>
        )}
      </header>

      <div class="ranges">
        {RANGES.map((r) => (
          <button
            key={r.seconds}
            class={`range-btn ${rangeSec === r.seconds ? 'active' : ''}`}
            onClick={() => setRangeSec(r.seconds)}
          >
            {r.label}
          </button>
        ))}
      </div>

      {err && !m && <div class="banner err">{err}</div>}
      {m?.metricsError && <div class="banner err">Live metrics unavailable: {m.metricsError}</div>}
      {loading && !m && <div class="panel-msg">Loading metrics…</div>}

      {m && (
        <>
          <div class="detail-charts">
            <MetricSection
              title="CPU"
              current={fmtCores(m.cpu.cores)}
              sub={m.cpu.limitCores != null ? `${fmtPct(m.cpu.pct)} of ${m.cpu.limitCores} cores` : ''}
              t={tone(m.cpu.pct, 70, 90)}
              series={m.spark.cpu}
              color="#6ea8fe"
              fmt={fmtCores}
            />
            <MetricSection
              title="Memory"
              current={fmtBytes(m.memory.bytes)}
              sub={m.memory.limitBytes != null ? `${fmtPct(m.memory.pct)} of ${fmtBytes(m.memory.limitBytes)}` : ''}
              t={tone(m.memory.pct, 80, 95)}
              series={m.spark.memory}
              color="#56d364"
              fmt={fmtBytes}
            />
            <MetricSection
              title="Disk"
              current={fmtBytes(m.disk.usedBytes)}
              sub={m.disk.capacityBytes != null ? `${fmtPct(m.disk.pct)} of ${fmtBytes(m.disk.capacityBytes)}` : ''}
              t={tone(m.disk.pct, 80, 90)}
              series={m.spark.disk}
              color="#e3b341"
              fmt={fmtBytes}
            />
          </div>

          <ResourceEditor
            user={user}
            cpuLimit={m.cpu.limitCores}
            memLimit={m.memory.limitBytes}
            onSaved={() => setReloadKey((k) => k + 1)}
          />

          {ws && <UpdatesCard ws={ws} onUpdated={() => setReloadKey((k) => k + 1)} />}

          <div class="detail-foot">
            <div>
              <span class="foot-k">Est. cost</span>{' '}
              {m.cost ? `${fmtUsd(m.cost.perMonth)}/mo` : '—'}
              {m.cost && (
                <span class="muted">
                  {' '}
                  ({fmtUsd(m.cost.computePerMonth)} compute + {fmtUsd(m.cost.storagePerMonth)} storage)
                </span>
              )}
            </div>
            <div>
              <span class="foot-k">Network</span> ↓ {fmtRate(m.network.rxBps)} · ↑{' '}
              {fmtRate(m.network.txBps)}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function MetricSection({
  title,
  current,
  sub,
  t,
  series,
  color,
  fmt,
}: {
  title: string;
  current: string;
  sub: string;
  t: 'ok' | 'warn' | 'crit';
  series: Series;
  color: string;
  fmt: (n: number) => string;
}) {
  return (
    <section class="metric-section">
      <div class="metric-head">
        <span class="metric-title">{title}</span>
        <span class={`metric-current tone-${t}`}>{current}</span>
        {sub && <span class="metric-sub">{sub}</span>}
      </div>
      <Chart points={series} color={color} fmt={fmt} />
    </section>
  );
}

function ResourceEditor({
  user,
  cpuLimit,
  memLimit,
  onSaved,
}: {
  user: string;
  cpuLimit: number | null;
  memLimit: number | null;
  onSaved: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [cpu, setCpu] = useState('');
  const [memory, setMemory] = useState('');
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  function start() {
    setCpu(toCpuQty(cpuLimit));
    setMemory(toMemQty(memLimit));
    setErr(null);
    setDone(false);
    setOpen(true);
  }

  async function save() {
    if (
      !window.confirm(
        `Apply new limits to ${user}? This patches the deployment and restarts ` +
          `the pod — any live terminal sessions or running tasks will be lost.`,
      )
    ) {
      return;
    }
    setSaving(true);
    setErr(null);
    try {
      await setWorkspaceResources(user, { cpu: cpu.trim() || undefined, memory: memory.trim() || undefined });
      setDone(true);
      setOpen(false);
      onSaved();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  if (!open) {
    return (
      <div class="res-edit-bar">
        <button class="btn ghost" onClick={start}>
          Edit limits
        </button>
        {done && <span class="res-edit-note">Limits updated — pod is rolling out.</span>}
        {err && <span class="res-edit-note err">{err}</span>}
      </div>
    );
  }

  return (
    <div class="res-editor">
      <div class="res-editor-fields">
        <label class="field">
          <span class="field-label">CPU limit</span>
          <input class="input" value={cpu} placeholder="2 or 500m" onInput={(e) => setCpu((e.target as HTMLInputElement).value)} />
        </label>
        <label class="field">
          <span class="field-label">Memory limit</span>
          <input class="input" value={memory} placeholder="4Gi or 512Mi" onInput={(e) => setMemory((e.target as HTMLInputElement).value)} />
        </label>
      </div>
      {err && <div class="banner err">{err}</div>}
      <p class="sub">Changing limits restarts the pod. Requests are left unchanged; durable changes belong in values.yaml.</p>
      <div class="res-editor-actions">
        <button class="btn start" disabled={saving} onClick={save}>
          {saving ? 'Applying…' : 'Apply & restart'}
        </button>
        <button class="btn ghost" disabled={saving} onClick={() => setOpen(false)}>
          Cancel
        </button>
      </div>
    </div>
  );
}

function UpdatesCard({ ws, onUpdated }: { ws: Workspace; onUpdated: () => void }) {
  const [busyUpdate, setBusyUpdate] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const latest = latestVersion.value;
  const current = ws.version ?? 'unknown';

  async function run() {
    if (
      !window.confirm(
        `Restart ${ws.user} and pull ${latest ?? 'the latest release'}? The pod ` +
          `restarts — running processes and unsaved in-memory state are lost; the ` +
          `/home/dev disk (PVC) is preserved.`,
      )
    ) {
      return;
    }
    setBusyUpdate(true);
    setErr(null);
    setNote(null);
    try {
      const r = await updateWorkspace(ws.user);
      setNote(
        `Updating ${r.fromVersion ?? '?'} → ${r.toVersion}. Pod is rolling out` +
          (r.persisted ? ' (pinned in GitOps).' : '.'),
      );
      onUpdated();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyUpdate(false);
    }
  }

  return (
    <div class="updates-card">
      <div class="updates-row">
        <div>
          <span class="field-label">Version</span>
          <div class="updates-version">
            {current}
            {ws.updateAvailable && latest && <span class="updates-arrow">→ {latest}</span>}
          </div>
        </div>
        <button class="btn start" disabled={busyUpdate || !ws.updateAvailable} onClick={run}>
          {busyUpdate ? 'Updating…' : ws.updateAvailable ? 'Restart & update' : 'Up to date'}
        </button>
      </div>
      <p class="sub">
        {ws.updateAvailable
          ? `A newer release (${latest}) is available. Updating patches the image tag and restarts the pod.`
          : latest
            ? `Running the latest release (${latest}).`
            : 'Latest-release lookup unavailable.'}
      </p>
      {note && <div class="res-edit-note">{note}</div>}
      {err && <div class="banner err">{err}</div>}
    </div>
  );
}
