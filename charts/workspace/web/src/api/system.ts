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

export interface ServerMode {
  readOnly: boolean;
  authed: boolean;
  authMode: 'basic' | 'oauth2' | 'none' | string;
  /** Public-demo "show everything" hint. When true (only on the read-only
   *  demo deploy), MutatorOnly renders mutation controls disabled instead of
   *  hiding them, so visitors see the full UI. The server still 403s writes. */
  demoShowAll?: boolean;
}

export const getMode = () => apiGet<ServerMode>('/api/mode');
