import { apiGet } from './client';

export interface Health {
  vscode?: boolean;
  terminal?: boolean;
  browser?: boolean;
  /** server.py adds an aggregate 'ok' field in some shapes; allow any */
  [k: string]: unknown;
}

export interface Metrics {
  cpu_percent?: number;
  memory_percent?: number;
  disk_percent?: number;
  timestamp?: number;
  [k: string]: unknown;
}

export const getHealth = () => apiGet<Health>('/health');
export const getMetrics = () => apiGet<Metrics>('/metrics');
