/**
 * Workspace pod system metrics. server.py mounts these at the top level
 * (/metrics, /health) — *not* under /api/ — so they don't go through the
 * withOauthPrefix() helper. We hit /oauth/metrics directly because the
 * auth-injecting ingress matches /oauth/(.*).
 */

export interface CpuMetrics {
  usage_percent: number;
  cores: number;
  error?: string;
}

export interface MemoryMetrics {
  total_mb: number;
  used_mb: number;
  available_mb: number;
  percent: number;
  error?: string;
}

export interface DiskMetrics {
  total_gb: number;
  used_gb: number;
  available_gb: number;
  percent: number;
  path: string;
  error?: string;
}

export interface MetricsAlert {
  type: 'critical' | 'warning';
  resource: 'cpu' | 'memory' | 'disk';
  message: string;
}

export interface SystemMetrics {
  cpu: CpuMetrics;
  memory: MemoryMetrics;
  disk: DiskMetrics;
  alerts: MetricsAlert[];
  timestamp: number;
}

export interface HealthService {
  status: 'up' | 'down';
  port: number;
}

export interface HealthSnapshot {
  status: 'healthy' | 'degraded' | 'down';
  services: Record<string, HealthService>;
  timestamp: number;
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path, { headers: { Accept: 'application/json' } });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as T;
}

export const fetchMetrics = () => getJson<SystemMetrics>('/oauth/metrics');
export const fetchHealth = () => getJson<HealthSnapshot>('/oauth/health');
