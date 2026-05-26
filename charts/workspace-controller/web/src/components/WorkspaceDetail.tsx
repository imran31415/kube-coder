import { useEffect, useState } from 'preact/hooks';
import { type Series, type WorkspaceMetrics, getWorkspaceMetrics } from '../api/workspaces';
import { findWorkspace, toggle, busy } from '../store';
import { navigate } from '../router';
import { Chart } from './Chart';
import { fmtBytes, fmtCores, fmtPct, fmtRate, fmtUptime, fmtUsd, tone } from '../format';

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
  }, [user, rangeSec]);

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
            {ws ? ws.deployment : `ws-${user}`}
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
