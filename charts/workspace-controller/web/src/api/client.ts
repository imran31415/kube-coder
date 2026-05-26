/**
 * Thin typed fetch wrapper. Copied verbatim from charts/workspace/web so the
 * controller SPA shares the dashboard's proven auth behavior: prepend /oauth
 * to /api/* (so the oauth2 cookie attaches), and on the "auth expired" symptom
 * bounce the page to /oauth2/start.
 *
 * For local dev a Bearer token from localStorage['kc.devToken'] is attached
 * when present (matches controller.py's CONTROLLER_DEV_TOKEN).
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

/**
 * Detect the "auth expired" symptom and bounce the whole page to
 * /oauth2/start?rd=<current> so the user sees a real sign-in prompt
 * instead of silent CORS errors in devtools. Fires at most once per page
 * load so we can't get stuck in a redirect loop if sign-in fails.
 */
let _authRedirectFired = false;
function redirectToSignIn(): void {
  if (_authRedirectFired) return;
  if (typeof window === 'undefined') return;
  _authRedirectFired = true;
  const rd = encodeURIComponent(window.location.pathname + window.location.search);
  window.location.href = `/oauth2/start?rd=${rd}`;
}

function isAuthExpiredError(e: unknown): boolean {
  // Browser fetch raises TypeError when it tries to follow a cross-origin
  // redirect with credentials (oauth2-proxy 302 → github.com/login/...).
  if (!(e instanceof TypeError)) return false;
  const msg = (e.message || '').toLowerCase();
  return msg.includes('failed to fetch') || msg.includes('networkerror') || msg.includes('load failed');
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
  let res: Response;
  try {
    res = await fetch(url, init);
  } catch (e) {
    if (isAuthExpiredError(e)) {
      redirectToSignIn();
      throw new ApiError('Session expired — redirecting to sign in.', 401, null);
    }
    throw e;
  }
  if (res.status === 401 && url.startsWith('/oauth/')) {
    redirectToSignIn();
    throw new ApiError('Session expired — redirecting to sign in.', 401, null);
  }
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
