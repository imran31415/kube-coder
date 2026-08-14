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

/**
 * Managed upload storage vs its cap (#556) — the `uploads/` and
 * `.claude-tasks/<task>/attachments/` dirs the dashboard and agents write into
 * on their own. Distinct from `DiskMetrics`, which is the whole PVC: uploads
 * can hit their cap on a mostly-empty volume. `quota_bytes: 0` means the cap
 * is disabled, and `percent` is then meaningless (0).
 */
export interface UploadMetrics {
  used_bytes: number;
  used_mb: number;
  quota_bytes: number;
  available_bytes: number | null;
  percent: number;
  dir_count: number;
  error?: string;
}

export interface MetricsAlert {
  type: 'critical' | 'warning';
  resource: 'cpu' | 'memory' | 'disk' | 'uploads';
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

/**
 * Per-model token counts, split by billing class (#574). Kept apart because the
 * classes price very differently — cache reads are far cheaper than fresh input,
 * cache writes carry a premium, output bills several times input.
 */
export interface TokenClassCounts {
  input: number;
  cache_read: number;
  cache_write: number;
  output: number;
  /** Pre-#574 tokens whose input classes were stored collapsed — counted, but
   *  not attributable to a class, so never priceable. */
  legacy_input_combined: number;
  /** input + cache_read + cache_write + output. */
  priceable_total: number;
  /** priceable_total + legacy_input_combined — every token counted. */
  total: number;
  records: number;
  by_model: Record<string, { input: number; cache_read: number; cache_write: number; output: number; records: number }>;
  sessions?: number;
  tasks?: number;
}

/** How much of the workspace's spend is measurable at all (#574) — a 0 from an
 *  uninstrumented assistant is not the same claim as a measured 0. */
export interface TokenCoverage {
  measured_assistants: string[];
  threads: { measured: number; not_instrumented: number; no_session_id: number };
  builds: { measured: number; not_instrumented: number; no_session_id: number };
}

export interface TokenMetrics {
  /** Hypervisor threads only, unchanged since #363: every token counted. */
  total: number;
  /** Hypervisor threads only: all input-side tokens combined. Use
   *  `threads.input` / `.cache_read` / `.cache_write` for the split. */
  input: number;
  output: number;
  per_session_avg: number;
  /** #574 — additive; absent on an older pod. */
  schema?: number;
  threads?: TokenClassCounts;
  builds?: TokenClassCounts;
  /** Threads + Builds: the first figure that includes autonomous Build spend. */
  all?: TokenClassCounts;
  coverage?: TokenCoverage;
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
  // Present on pods running the #556 build; older pods omit it.
  uploads?: UploadMetrics;
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
