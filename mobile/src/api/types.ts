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
