import { apiGet, apiPost } from './client';

export type WorkspaceState = 'running' | 'stopped' | 'transitioning' | 'degraded';

export interface PodSummary {
  name: string;
  phase: string;
  ready: boolean;
  restarts: number;
  reason: string | null;
}

export interface Workspace {
  /** Username (deployment name minus the ws- prefix). */
  user: string;
  /** The Kubernetes Deployment name, e.g. ws-imran. */
  deployment: string;
  state: WorkspaceState;
  desiredReplicas: number;
  readyReplicas: number;
  /** The workspace's own URL (from its ingress host), or null. */
  url: string | null;
  pods: PodSummary[];
  /** Short human-readable status, e.g. "1/1 ready" or "CrashLoopBackOff". */
  detail: string;
  /** Full ide-container image ref, or null. */
  image: string | null;
  /** Raw image tag, e.g. "devlaptop-v1.4.0", or null. */
  imageTag: string | null;
  /** Semver version parsed from the tag, e.g. "v1.4.0"; null if not pinned. */
  version: string | null;
  /** True when a newer release exists than this workspace's version. */
  updateAvailable: boolean;
}

export interface WorkspacesResponse {
  namespace: string;
  workspaces: Workspace[];
  /** Latest released version ("v1.4.0"), or null when the lookup failed. */
  latestVersion: string | null;
}

export const listWorkspaces = () => apiGet<WorkspacesResponse>('/api/workspaces');

/** A series of [unixSeconds, value] points for a sparkline. */
export type Series = [number, number][];

export interface WorkspaceMetrics {
  user: string;
  running: boolean;
  cpu: { cores: number | null; limitCores: number | null; pct: number | null };
  memory: { bytes: number | null; limitBytes: number | null; pct: number | null };
  disk: { usedBytes: number | null; capacityBytes: number | null; pct: number | null };
  network: { rxBps: number | null; txBps: number | null };
  uptimeSeconds: number | null;
  cost: {
    perHour: number;
    computePerMonth: number;
    storagePerMonth: number;
    perMonth: number;
  } | null;
  spark: { rangeSeconds: number; step: number; cpu: Series; memory: Series; disk: Series };
  /** Non-null when Prometheus was unreachable; the rest degrades to nulls. */
  metricsError: string | null;
}

export const getWorkspaceMetrics = (user: string, rangeSeconds = 3600) =>
  apiGet<WorkspaceMetrics>(`/api/workspaces/${user}/metrics`, { range: rangeSeconds });

export type Severity = 'critical' | 'warn' | 'info';

export interface Advisory {
  user: string;
  severity: Severity;
  kind: string;
  message: string;
}

export interface InsightsResponse {
  generatedAt: number;
  windowSeconds: number;
  advisories: Advisory[];
  error: string | null;
}

export const getInsights = () => apiGet<InsightsResponse>('/api/insights');

export const startWorkspace = (user: string) =>
  apiPost<{ ok: true; user: string; desiredReplicas: number }>(`/api/workspaces/${user}/start`);

export const stopWorkspace = (user: string) =>
  apiPost<{ ok: true; user: string; desiredReplicas: number }>(`/api/workspaces/${user}/stop`);

/** Patch the workspace's ide-container CPU/memory limits. Triggers a rollout. */
export const setWorkspaceResources = (user: string, limits: { cpu?: string; memory?: string }) =>
  apiPost<{ ok: true; user: string; limits: { cpu?: string; memory?: string } }>(
    `/api/workspaces/${user}/resources`,
    limits,
  );

export interface UpdateResult {
  ok: true;
  user: string;
  fromVersion: string | null;
  toVersion: string;
  imageTag: string;
  image: string;
  rolled: boolean;
  persisted: boolean;
  persistError: string | null;
}

/**
 * Restart the workspace onto a release image (latest by default). Patches the
 * live Deployment and, when GitOps is configured, persists the new tag.
 */
export const updateWorkspace = (user: string, version?: string) =>
  apiPost<UpdateResult>(`/api/workspaces/${user}/update`, version ? { version } : {});
