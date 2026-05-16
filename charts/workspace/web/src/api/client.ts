/**
 * Thin typed fetch wrapper. The Flask-ish server.py answers JSON for everything
 * under /api/* and uses non-2xx status codes for errors.
 *
 * Auth: in production, the workspace ingress only injects X-Auth-Request-*
 * headers for paths under /oauth/*. Routes that hit /api/foo directly skip
 * oauth2 and arrive unauthenticated, so we prepend /oauth to every /api/ call
 * via withOauthPrefix(). server.py strips the prefix (server.py:1931, 3381),
 * so dev_server (which monkey-patches auth to always-true) is unaffected.
 *
 * For ad-hoc / programmatic use a Bearer token from localStorage['kc.devToken']
 * is attached when present.
 */

export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;
  constructor(message: string, status: number, body: unknown) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

interface Options extends Omit<RequestInit, 'body'> {
  body?: unknown;
  query?: Record<string, string | number | boolean | undefined | null>;
}

function devToken(): string | null {
  try {
    return typeof localStorage !== 'undefined' ? localStorage.getItem('kc.devToken') : null;
  } catch {
    return null;
  }
}

/**
 * Prepend /oauth to /api/* paths so requests go through the auth-injecting
 * ingress in production. Absolute URLs and already-prefixed paths pass through.
 * Exported so raw fetch()/EventSource callers (files.ts, SSE in tasks.ts) can
 * apply the same rule without duplicating the logic.
 */
export function withOauthPrefix(path: string): string {
  if (/^https?:\/\//i.test(path)) return path;
  if (path.startsWith('/oauth/')) return path;
  if (path.startsWith('/api/')) return `/oauth${path}`;
  return path;
}

function buildUrl(path: string, query?: Options['query']): string {
  const prefixed = withOauthPrefix(path);
  if (!query) return prefixed;
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === '') continue;
    params.set(k, String(v));
  }
  const qs = params.toString();
  return qs ? `${prefixed}?${qs}` : prefixed;
}

export async function api<T = unknown>(path: string, opts: Options = {}): Promise<T> {
  const { body, query, headers, ...rest } = opts;
  const init: RequestInit = {
    ...rest,
    headers: {
      'Accept': 'application/json',
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      ...(devToken() ? { Authorization: `Bearer ${devToken()}` } : {}),
      ...(headers as Record<string, string> | undefined),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  };
  const url = buildUrl(path, query);
  const res = await fetch(url, init);
  const ctype = res.headers.get('Content-Type') || '';
  const isJson = ctype.includes('application/json');
  const parsed: unknown = isJson ? await res.json().catch(() => null) : await res.text();
  if (!res.ok) {
    const msg =
      (isJson && parsed && typeof parsed === 'object' && 'error' in (parsed as Record<string, unknown>)
        ? String((parsed as Record<string, unknown>).error)
        : null) ?? `${res.status} ${res.statusText}`;
    throw new ApiError(msg, res.status, parsed);
  }
  return parsed as T;
}

export const apiGet = <T>(path: string, query?: Options['query']) => api<T>(path, { method: 'GET', query });
export const apiPost = <T>(path: string, body?: unknown) => api<T>(path, { method: 'POST', body });
export const apiDelete = <T>(path: string) => api<T>(path, { method: 'DELETE' });
