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
