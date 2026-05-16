/**
 * Thin typed fetch wrapper. The Flask-ish server.py answers JSON for everything
 * under /api/* and uses non-2xx status codes for errors.
 *
 * Auth: in the workspace pod the OAuth2 ingress injects X-Auth-Request-* headers
 * — we just pass requests through, the browser sends them automatically.
 * For ad-hoc / programmatic use we can attach a Bearer token from
 * localStorage['kc.devToken']. The dev_server bypasses auth entirely.
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

function buildUrl(path: string, query?: Options['query']): string {
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === '') continue;
    params.set(k, String(v));
  }
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
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
