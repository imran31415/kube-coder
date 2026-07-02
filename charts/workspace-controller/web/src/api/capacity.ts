import { apiGet } from './client';
import type { Series } from './workspaces';

// Mirrors controller.py:cluster_capacity. Every number is sourced from
// Prometheus (node allocatable from kube-state-metrics, usage from cadvisor),
// so the panel degrades to `metricsError` rather than failing when Prometheus
// is unreachable — same contract as the per-workspace metrics endpoint.

/** One resource's rollup against a node's (or the cluster's) allocatable. */
export interface ResourceRollup {
  /** Schedulable capacity, or null when kube-state-metrics has no series. */
  allocatable: number | null;
  /** Usage attributable to ws-* workspaces. */
  workspace: number;
  /** Usage of all scheduled workload (every namespace) on this capacity. */
  cluster: number;
  /** cluster − workspace: other tenants sharing the node. Null if unknown. */
  other: number | null;
  /** workspace / allocatable, as a percentage. Null when allocatable is unknown. */
  workspacePct: number | null;
  /** cluster / allocatable, as a percentage. Null when allocatable is unknown. */
  clusterPct: number | null;
}

export interface PodRollup {
  allocatable: number | null;
  workspace: number;
  cluster: number;
}

export interface NodeCapacity {
  name: string;
  cpu: ResourceRollup;
  memory: ResourceRollup;
  pods: PodRollup;
}

export interface ClusterCapacity {
  nodeCount: number;
  cpu: ResourceRollup;
  memory: ResourceRollup;
  pods: PodRollup;
}

export interface CapacityHistory {
  rangeSeconds: number;
  step: number;
  cpu: { allocatable: Series; workspace: Series; cluster: Series };
  memory: { allocatable: Series; workspace: Series; cluster: Series };
}

export interface CapacityResponse {
  generatedAt: number;
  namespace: string;
  /** Null when Prometheus was unreachable (see metricsError). */
  cluster: ClusterCapacity | null;
  nodes: NodeCapacity[];
  history: CapacityHistory;
  metricsError: string | null;
}

export const getCapacity = (rangeSeconds = 3600) =>
  apiGet<CapacityResponse>('/api/capacity', { range: rangeSeconds });

/** Overall cluster traffic-light for the landing page. */
export type HealthStatus = 'ok' | 'warn' | 'crit' | 'unknown';

/** Cheap cluster-health summary (controller.py:cluster_health) — cluster CPU +
 *  memory rollups and a status from a handful of instant queries, with no
 *  per-node breakdown or range history. Backs the summary page so it doesn't
 *  fire the heavy capacity/insights queries on every load. */
export interface ClusterHealthResponse {
  generatedAt: number;
  namespace: string;
  cluster: { nodeCount: number; cpu: ResourceRollup; memory: ResourceRollup } | null;
  status: HealthStatus;
  metricsError: string | null;
}

export const getCapacitySummary = () => apiGet<ClusterHealthResponse>('/api/capacity/summary');
