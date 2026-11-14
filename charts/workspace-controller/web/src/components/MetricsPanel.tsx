import { usePollingResource } from '../hooks/usePollingResource';
import { ResourceStatus } from './ResourceStatus';
import { getWorkspaceMetrics } from '../api/workspaces';
import { Card } from './Card';
import { fmtBytes, fmtCores, fmtPct, fmtRate, fmtUptime, fmtUsd, tone } from '../format';

/** Per-workspace mini dashboard, shown when a row is expanded. Loads on mount
 *  and refreshes every 10s while open. */
export function MetricsPanel({ user }: { user: string }) {
  const resource = usePollingResource(`workspace:${user}`, signal => getWorkspaceMetrics(user, 3600, signal), 10000);
  const m = resource.data;
  if (!m || (resource.problem && !resource.updatedAt)) return <div class="panel"><ResourceStatus {...resource} /></div>;

  return (
    <div class="panel">
      <ResourceStatus {...resource} />
      <div class="cards">
        <Card
          title="CPU"
          value={fmtCores(m.cpu.cores)}
          sub={m.cpu.limitCores != null ? `of ${m.cpu.limitCores} cores · ${fmtPct(m.cpu.pct)}` : ''}
          t={tone(m.cpu.pct, 70, 90)}
          spark={m.spark.cpu}
          sparkColor="#6ea8fe"
        />
        <Card
          title="Memory"
          value={fmtBytes(m.memory.bytes)}
          sub={m.memory.limitBytes != null ? `of ${fmtBytes(m.memory.limitBytes)} · ${fmtPct(m.memory.pct)}` : ''}
          t={tone(m.memory.pct, 80, 95)}
          spark={m.spark.memory}
          sparkColor="#56d364"
        />
        <Card
          title="Disk"
          value={fmtBytes(m.disk.usedBytes)}
          sub={m.disk.capacityBytes != null ? `of ${fmtBytes(m.disk.capacityBytes)} · ${fmtPct(m.disk.pct)}` : ''}
          t={tone(m.disk.pct, 80, 90)}
          spark={m.spark.disk}
          sparkColor="#e3b341"
        />
        <Card
          title="Est. cost"
          value={m.cost ? `${fmtUsd(m.cost.perMonth)}/mo` : '—'}
          sub={m.cost ? `compute ${fmtUsd(m.cost.computePerMonth)} · storage ${fmtUsd(m.cost.storagePerMonth)}` : ''}
          t="ok"
        />
      </div>
      <div class="panel-meta">
        <span>uptime {fmtUptime(m.uptimeSeconds)}</span>
        <span>net ↓ {fmtRate(m.network.rxBps)} · ↑ {fmtRate(m.network.txBps)}</span>
        {!m.running && <span class="muted">stopped — compute idle, storage still billed</span>}
        <a class="panel-link" href={`#/w/${user}`}>Open detailed metrics →</a>
      </div>
    </div>
  );
}
