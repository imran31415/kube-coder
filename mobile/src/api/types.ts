/** Shared API types mirroring server.py's JSON shapes. */

export interface TaskSummary {
  id: string;
  prompt: string;
  status: string; // running | waiting | done | error | killed
  assistant?: string;
  workdir?: string;
  created_at?: number;
  updated_at?: number;
  waiting_for_input?: boolean;
}

export interface TaskDetail extends TaskSummary {
  output?: string;
  tmux_session?: string;
}

export interface MemoryRecord {
  namespace: string;
  key: string;
  value: string;
  tags?: string[];
  importance?: number;
  updated_at?: number;
}

export interface Metrics {
  cpu_percent: number;
  memory_used_mb: number;
  memory_total_mb: number;
  disk_used_gb: number;
  disk_total_gb: number;
}

export interface Health {
  vscode?: boolean;
  terminal?: boolean;
  browser?: boolean;
  ok?: boolean;
}

/** Desktop launcher (mirrors /api/desktop — the web dashboard's Desktop tab). */
export type DesktopActionType = 'task' | 'url' | 'shell';

export interface DesktopActionTask {
  type: 'task';
  prompt: string;
  workdir?: string;
  assistant?: string;
}
export interface DesktopActionUrl {
  type: 'url';
  url: string;
  target: 'blank' | 'self';
}
export interface DesktopActionShell {
  type: 'shell';
  command: string;
  timeout?: number;
}
export type DesktopAction = DesktopActionTask | DesktopActionUrl | DesktopActionShell;

export interface DesktopItem {
  id: string;
  label: string;
  /** Emoji/text, or "icon:NAME" for a named line icon. */
  icon: string;
  /** Web-only keyboard shortcut; preserved (not editable) on mobile. */
  hotkey?: string;
  action: DesktopAction;
}

export type DesktopItemDraft = Omit<DesktopItem, 'id'>;

export type LaunchResult =
  | { kind: 'task'; task_id: string }
  | { kind: 'shell'; exit_code: number; stdout: string; stderr: string }
  | { kind: 'url'; url: string; target: 'blank' | 'self' };

/** One entry on the Applications tab (mirrors /api/apps). */
export interface AppEntry {
  port: number;
  /** Pinned name, or '' for an auto-discovered listener. */
  name: string;
  pinned: boolean;
  /** running (listening now) | stopped (pinned, not listening) | blocked (reserved port). */
  status: 'running' | 'stopped' | 'blocked';
  strip_prefix: boolean;
  /** Bind address from /proc/net/tcp, e.g. "127.0.0.1". */
  addr: string;
}

// ── Controller (admin plane) ────────────────────────────────────────────────
// Mirrors the workspace-controller's /api/workspaces and /api/capacity/summary.

export type WorkspaceState = 'running' | 'stopped' | 'transitioning' | 'degraded';

/** One workspace in the controller's list (mirrors controller.py list_workspaces). */
export interface ControllerWorkspace {
  user: string;
  deployment: string;
  namespace: string;
  isolated: boolean;
  state: WorkspaceState;
  desiredReplicas: number;
  readyReplicas: number;
  url: string | null;
  /** Short human status, e.g. "1/1 ready" or "CrashLoopBackOff". */
  detail: string;
  version: string | null;
  updateAvailable: boolean;
}

export interface ControllerWorkspacesResponse {
  namespace: string;
  workspaces: ControllerWorkspace[];
  latestVersion: string | null;
}

/** One resource rollup in the capacity summary (percent-of-cluster). */
export interface CapacityResource {
  clusterPct: number | null;
  workspacePct: number | null;
}

/** Cheap cluster-health rollup (mirrors controller.py cluster_health). */
export interface ControllerCapacity {
  generatedAt: number;
  namespace: string;
  status: 'ok' | 'warn' | 'critical' | 'unknown';
  metricsError: string | null;
  cluster: {
    nodeCount: number;
    cpu: CapacityResource;
    memory: CapacityResource;
  } | null;
}
