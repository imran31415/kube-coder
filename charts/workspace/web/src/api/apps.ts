import { apiGet, apiPost, apiDelete, authPrefix } from './client';

/** One row on the Applications page. */
export interface AppEntry {
  port: number;
  /** Pinned name, or empty string for an auto-discovered entry. */
  name: string;
  pinned: boolean;
  /** "running" (listening now), "stopped" (pinned but not listening),
   *  or "blocked" (pinned, but the port is reserved for the workspace). */
  status: 'running' | 'stopped' | 'blocked';
  /** Server-side flag — true means "pass /api/app-proxy/<port> through
   *  to the upstream as-is". Used for Vite-style apps configured with a
   *  --base prefix matching the proxy path. */
  strip_prefix: boolean;
  /** Bind address from /proc/net/tcp, e.g. "127.0.0.1" or "0.0.0.0". */
  addr: string;
}

export interface AppsListResponse {
  apps: AppEntry[];
  /** When non-null, the server is refusing to render embedded apps —
   *  typically because AUTH_MODE != oauth2, which means iframe
   *  sub-resource requests can't authenticate. The SPA surfaces this
   *  string as a banner instead of pretending the feature works. */
  unavailable_reason: string | null;
  auth_mode: string;
}

export interface PinRequest {
  port: number;
  name: string;
  strip_prefix?: boolean;
}

export const listApps = () => apiGet<AppsListResponse>('/api/apps');

export const pinApp = (req: PinRequest) =>
  apiPost<{ ok: true; pin: { port: number; name: string; strip_prefix: boolean } }>(
    '/api/apps/pins',
    req,
  );

export const unpinApp = (port: number) =>
  apiDelete<{ ok: true; removed: boolean }>(`/api/apps/pins/${port}`);

/** Path the proxy lives at — used for iframe src + "open in new tab".
 *  Uses the deployment's auth prefix (/oauth behind oauth2, empty under basic
 *  auth) so cookie/basic auth attaches against the right ingress. */
export const proxyUrl = (port: number, suffix = '/') =>
  `${authPrefix()}/api/app-proxy/${port}${suffix.startsWith('/') ? suffix : `/${suffix}`}`;
