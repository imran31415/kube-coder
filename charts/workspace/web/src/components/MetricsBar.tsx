import { useEffect } from 'preact/hooks';
import { metrics, startMetricsPolling } from '../store/metrics';
import './MetricsBar.css';

function tone(percent: number): 'ok' | 'warn' | 'danger' {
  if (percent >= 90) return 'danger';
  if (percent >= 75) return 'warn';
  return 'ok';
}

/**
 * Compact pod-resource readout in the topbar: three pills (CPU / Mem / Disk)
 * showing percentage usage with color-coded tone. Polls every 10 s via the
 * shared metrics store. Hidden on small screens (handled by CSS).
 */
export function MetricsBar() {
  useEffect(() => {
    startMetricsPolling(10000);
    // Don't stop on unmount — Topbar is always mounted; keep the poll global.
  }, []);

  const m = metrics.value;
  if (!m) {
    return (
      <div class="mbar" aria-label="System metrics loading">
        <span class="mbar-cell mbar-cell-loading mono">CPU —</span>
        <span class="mbar-cell mbar-cell-loading mono">MEM —</span>
        <span class="mbar-cell mbar-cell-loading mono">DSK —</span>
      </div>
    );
  }
  const cpu = m.cpu.usage_percent;
  const mem = m.memory.percent;
  const dsk = m.disk.percent;
  return (
    <div class="mbar" role="group" aria-label="Pod system metrics">
      <span
        class={`mbar-cell mbar-cell-${tone(cpu)}`}
        title={`CPU ${cpu}% across ${m.cpu.cores} core${m.cpu.cores === 1 ? '' : 's'}`}
      >
        <span class="mbar-dot" aria-hidden="true" />
        <span class="mbar-label">CPU</span>
        <span class="mono">{Math.round(cpu)}%</span>
      </span>
      <span
        class={`mbar-cell mbar-cell-${tone(mem)}`}
        title={`Memory ${m.memory.used_mb.toFixed(0)} / ${m.memory.total_mb.toFixed(0)} MB`}
      >
        <span class="mbar-dot" aria-hidden="true" />
        <span class="mbar-label">MEM</span>
        <span class="mono">{Math.round(mem)}%</span>
      </span>
      <span
        class={`mbar-cell mbar-cell-${tone(dsk)}`}
        title={`Disk ${m.disk.used_gb.toFixed(1)} / ${m.disk.total_gb.toFixed(1)} GB (${m.disk.path})`}
      >
        <span class="mbar-dot" aria-hidden="true" />
        <span class="mbar-label">DSK</span>
        <span class="mono">{Math.round(dsk)}%</span>
      </span>
    </div>
  );
}
