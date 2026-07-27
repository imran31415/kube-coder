import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { SystemMetrics } from '../api/metrics';

// Drive fetchMetrics from a per-test value; health is a constant stub.
let metricsResult: () => SystemMetrics;
vi.mock('../api/metrics', () => ({
  fetchMetrics: () => Promise.resolve(metricsResult()),
  fetchHealth: () =>
    Promise.resolve({ status: 'healthy', services: {}, timestamp: 0 }),
}));

import { metrics, refreshMetrics } from './metrics';

const SYSTEM: SystemMetrics = {
  cpu: { usage_percent: 1, cores: 4 },
  memory: { total_mb: 100, used_mb: 40, available_mb: 60, percent: 40 },
  disk: { total_gb: 10, used_gb: 4, available_gb: 6, percent: 40, path: '/home/dev' },
  alerts: [],
  timestamp: 0,
};

describe('metrics store — product section (#363)', () => {
  beforeEach(() => {
    metrics.value = null;
  });

  it('carries the product usage section through to the signal', async () => {
    metricsResult = () => ({
      ...SYSTEM,
      product: {
        chats: { total: 3, active: 1 },
        tokens: { total: 100, input: 60, output: 40, per_session_avg: 50 },
        skills: { invocations_by_name: { deploy: 2, graphify: 1 } },
        memory: {
          recall_count_by_key: [
            { namespace: 'proj', key: 'alpha', count: 5, last_accessed_at: 1 },
          ],
        },
      },
    });
    await refreshMetrics();
    const p = metrics.value?.product;
    expect(p?.chats).toEqual({ total: 3, active: 1 });
    expect(p?.tokens.per_session_avg).toBe(50);
    expect(p?.skills.invocations_by_name.deploy).toBe(2);
    expect(p?.memory.recall_count_by_key[0].key).toBe('alpha');
  });

  it('tolerates a pod whose /metrics omits product (backward compat)', async () => {
    metricsResult = () => ({ ...SYSTEM });
    await refreshMetrics();
    expect(metrics.value).not.toBeNull();
    // The system section still lands; product is simply absent.
    expect(metrics.value?.product).toBeUndefined();
    expect(metrics.value?.cpu.cores).toBe(4);
  });
});
