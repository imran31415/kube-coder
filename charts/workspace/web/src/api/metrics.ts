/**
 * Workspace pod system metrics. server.py mounts these at the top level
 * (/metrics, /health) — *not* under /api/ — so they don't go through the
 * withOauthPrefix() helper. We prefix with authPrefix() (/oauth behind the
 * oauth2 ingress, empty under basic auth) so they hit the same ingress the
 * SPA was served from.
 */
import { authPrefix } from './client';

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

/**
 * Product-usage metrics (#363) — how the product is *used*, distinct from the
 * system cpu/memory/disk above. NB: `ProductMetrics.memory` is the knowledge
 * store's recall counts, NOT RAM (that's `SystemMetrics.memory`). All fields are
 * optional/defaulted so an older pod that omits `product` degrades gracefully.
 */
export interface ChatMetrics {
  total: number;
  active: number;
}

export interface TokenMetrics {
  total: number;
  input: number;
  output: number;
  per_session_avg: number;
}

export interface SkillMetrics {
  invocations_by_name: Record<string, number>;
}

export interface MemoryRecall {
  namespace: string;
  key: string;
  count: number;
  last_accessed_at?: number | null;
}

export interface ProductMemoryMetrics {
  recall_count_by_key: MemoryRecall[];
}

export interface ProductMetrics {
  chats: ChatMetrics;
  tokens: TokenMetrics;
  skills: SkillMetrics;
  memory: ProductMemoryMetrics;
}

export interface SystemMetrics {
  cpu: CpuMetrics;
  memory: MemoryMetrics;
  disk: DiskMetrics;
  alerts: MetricsAlert[];
  timestamp: number;
  // Present on pods running the #363 build; older pods omit it.
  product?: ProductMetrics;
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

export const fetchMetrics = () => getJson<SystemMetrics>(`${authPrefix()}/metrics`);
export const fetchHealth = () => getJson<HealthSnapshot>(`${authPrefix()}/health`);
