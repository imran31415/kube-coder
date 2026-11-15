import { usePollingResource } from '../hooks/usePollingResource';
import { ResourceStatus } from './ResourceStatus';
import { useState } from 'preact/hooks';
import { type NodeCapacity, getCapacity } from '../api/capacity';
import { CapacityBar } from './CapacityBar';
import { CapacityChart } from './CapacityChart';
import { fmtBytes, fmtCores, fmtPct } from '../format';

const RANGES: [string, number][] = [
  ['1h', 3600],
  ['6h', 21600],
  ['24h', 86400],
  ['7d', 604800],
];

/** Top-level cluster/node capacity rollup. Polls every 15s; the range selector
 *  drives the history window. Loads on mount and keeps the last good data
 *  through a transient poll failure so the panel never flickers to empty. */
export function CapacityPanel() {
  const [range, setRange] = useState(3600);
  const [res, setRes] = useState<'cpu' | 'memory'>('cpu');
  const [open, setOpen] = useState(true);

  const resource = usePollingResource(`capacity:${range}`, signal => getCapacity(range, signal), 15000);
  const cap = resource.data;

  if (!cap) return <section class="cap"><div class="cap-hd"><h2>Cluster capacity</h2></div><ResourceStatus {...resource} /></section>;

  const c = cap.cluster;
  const fmt = res === 'cpu' ? fmtCores : fmtBytes;
  const hist = cap.history[res];

  return (
    <section class="cap">
      <div class="cap-hd">
        <button class="cap-toggle" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
          <span class={`chev ${open ? 'open' : ''}`} aria-hidden="true">▸</span>
          <h2>Cluster capacity</h2>
        </button>
        {c && (
          <span class="cap-sub">
            {c.nodeCount} node{c.nodeCount === 1 ? '' : 's'} · {c.pods.workspace} workspace pods of {c.pods.cluster} scheduled
          </span>
        )}
        <div class="cap-ranges">
          {RANGES.map(([label, secs]) => (
            <button
              key={secs}
              class={`chip ${range === secs ? 'on' : ''}`}
              onClick={() => setRange(secs)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      <ResourceStatus {...resource} />

      {open && c && (!resource.problem || resource.updatedAt) && (
        <>
          <div class="cap-summary">
            <div class="cap-metric">
              <div class="cap-metric-hd">
                <span class="cap-metric-name">CPU</span>
                <span class="cap-metric-val">{fmtCores(c.cpu.cluster)} / {fmtCores(c.cpu.allocatable)} · {fmtPct(c.cpu.clusterPct)} used</span>
              </div>
              <CapacityBar rollup={c.cpu} fmt={fmtCores} />
            </div>
            <div class="cap-metric">
              <div class="cap-metric-hd">
                <span class="cap-metric-name">Memory</span>
                <span class="cap-metric-val">{fmtBytes(c.memory.cluster)} / {fmtBytes(c.memory.allocatable)} · {fmtPct(c.memory.clusterPct)} used</span>
              </div>
              <CapacityBar rollup={c.memory} fmt={fmtBytes} warn={80} crit={92} />
            </div>
          </div>

          <div class="cap-chart">
            <div class="cap-chart-hd">
              <span class="cap-metric-name">History</span>
              <div class="cap-ranges">
                <button class={`chip ${res === 'cpu' ? 'on' : ''}`} onClick={() => setRes('cpu')}>CPU</button>
                <button class={`chip ${res === 'memory' ? 'on' : ''}`} onClick={() => setRes('memory')}>Memory</button>
              </div>
            </div>
            <CapacityChart workspace={hist.workspace} cluster={hist.cluster} allocatable={hist.allocatable} fmt={fmt} />
          </div>

          {cap.nodes.length > 0 && (
            <div class="cap-nodes">
              <div class="cap-nodes-hd">Per-node</div>
              {cap.nodes.map((n) => (
                <NodeRow key={n.name} node={n} />
              ))}
            </div>
          )}
        </>
      )}
    </section>
  );
}

function NodeRow({ node }: { node: NodeCapacity }) {
  return (
    <div class="cap-node">
      <div class="cap-node-name">
        {node.name}
        <span class="cap-node-pods">{node.pods.workspace}/{node.pods.cluster} pods</span>
      </div>
      <div class="cap-node-bars">
        <div class="cap-node-bar">
          <span class="cap-node-tag">CPU</span>
          <CapacityBar rollup={node.cpu} fmt={fmtCores} />
        </div>
        <div class="cap-node-bar">
          <span class="cap-node-tag">Mem</span>
          <CapacityBar rollup={node.memory} fmt={fmtBytes} warn={80} crit={92} />
        </div>
      </div>
    </div>
  );
}
