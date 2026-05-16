import { signal } from '@preact/signals';
import { fetchMetrics, fetchHealth, type SystemMetrics, type HealthSnapshot } from '../api/metrics';

export const metrics = signal<SystemMetrics | null>(null);
export const health = signal<HealthSnapshot | null>(null);
export const metricsError = signal<string | null>(null);
export const metricsLastFetch = signal<number | null>(null);

let pollTimer: number | null = null;

export async function refreshMetrics(): Promise<void> {
  try {
    const [m, h] = await Promise.allSettled([fetchMetrics(), fetchHealth()]);
    if (m.status === 'fulfilled') {
      metrics.value = m.value;
      metricsError.value = null;
    } else {
      metricsError.value = m.reason instanceof Error ? m.reason.message : 'metrics fetch failed';
    }
    if (h.status === 'fulfilled') health.value = h.value;
    metricsLastFetch.value = Date.now();
  } catch (e) {
    metricsError.value = e instanceof Error ? e.message : 'metrics fetch failed';
  }
}

/** Start polling at the given interval (default 10 s). Safe to call repeatedly. */
export function startMetricsPolling(intervalMs = 10000): void {
  if (pollTimer != null) return;
  void refreshMetrics();
  pollTimer = window.setInterval(refreshMetrics, intervalMs);
}

export function stopMetricsPolling(): void {
  if (pollTimer != null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}
