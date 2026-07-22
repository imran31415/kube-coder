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

/**
 * Type guard for `{ error: string }`-shaped API responses. Call sites must use
 * this instead of a bare `'error' in r` — `api()` returns plain text bodies
 * as a string (client.ts:118), and applying `in` to a string throws
 * `TypeError: Cannot use 'in' operator…`, which then surfaces in a toast
 * (issue #44).
 */
export function isErrorResponse(v: unknown): v is { error: string } {
  return typeof v === 'object' && v !== null && 'error' in v;
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
 * The external path prefix the dashboard is served under, detected from where
 * the SPA itself was loaded:
 *   - '/oauth'  behind the oauth2-proxy ingress (the auth-injecting ingress
 *               only matches /oauth/*, and the user's URL is /oauth/...).
 *   - ''        a root / http-basic-auth deployment (e.g. local minikube),
 *               where there is no /oauth route and services live at /terminal,
 *               /vscode, /api, … directly.
 * Using the loaded prefix keeps API calls AND embedded-service URLs (terminal,
 * VS Code, VNC, metrics, app-proxy) pointed at the same ingress the user
 * authenticated against, instead of hardcoding /oauth and 404ing under basic
 * auth.
 */
export function authPrefix(): string {
  // server.py injects window.__KC_AUTH_PREFIX__ into the served index.html from
  // the deployment's AUTH_MODE: '/oauth' behind the oauth2 ingress (only
  // /oauth/* paths get the auth header injected), '' for a root/basic-auth
  // deployment. The SPA is served at '/' in BOTH modes, so the URL can't tell
  // them apart — the server is the source of truth. Default to '/oauth' (the
  // production deployment) when the injection is absent so a stray build never
  // silently drops the prefix and 401s every API call.
  if (typeof window !== 'undefined') {
    const inj = (window as { __KC_AUTH_PREFIX__?: unknown }).__KC_AUTH_PREFIX__;
    if (typeof inj === 'string') return inj;
  }
  return '/oauth';
}

/**
 * Prepend the deployment's auth prefix to /api/* paths so requests go through
 * the same ingress the SPA was served from. Absolute URLs and already-prefixed
 * paths pass through. Exported so raw fetch()/EventSource callers (files.ts,
 * SSE in tasks.ts) can apply the same rule without duplicating the logic.
 */
export function withOauthPrefix(path: string): string {
  if (/^https?:\/\//i.test(path)) return path;
  if (path.startsWith('/oauth/')) return path;
  if (path.startsWith('/api/')) return `${authPrefix()}${path}`;
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
  // No status code reaches us — only a generic "Failed to fetch".
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
  // oauth2-proxy sometimes returns 401 directly (when configured to skip the
  // redirect for AJAX) — handle that path too.
  if (res.status === 401 && url.startsWith('/oauth/')) {
    redirectToSignIn();
    throw new ApiError('Session expired — redirecting to sign in.', 401, null);
  }
  const ctype = res.headers.get('Content-Type') || '';
  const isJson = ctype.includes('application/json');
  const parsed: unknown = isJson ? await res.json().catch(() => null) : await res.text();
  if (!res.ok) {
    const msg =
      (isJson && isErrorResponse(parsed) ? String(parsed.error) : null) ??
      `${res.status} ${res.statusText}`;
    throw new ApiError(msg, res.status, parsed);
  }
  return parsed as T;
}

export const apiGet = <T>(path: string, query?: Options['query']) => api<T>(path, { method: 'GET', query });
export const apiPost = <T>(path: string, body?: unknown) => api<T>(path, { method: 'POST', body });
export const apiPut = <T>(path: string, body?: unknown) => api<T>(path, { method: 'PUT', body });
export const apiDelete = <T>(path: string) => api<T>(path, { method: 'DELETE' });

/**
 * Like `api()` but for non-JSON bodies (file uploads). Skips JSON
 * serialisation but applies the same auth header + oauth prefix +
 * session-expired redirect behaviour. Use for endpoints that take a
 * raw Blob/File/FormData.
 */
export async function apiRaw(
  path: string,
  opts: { method?: string; body?: BodyInit; headers?: Record<string, string> } = {},
): Promise<Response> {
  const url = buildUrl(path);
  const init: RequestInit = {
    method: opts.method || 'POST',
    headers: {
      ...(devToken() ? { Authorization: `Bearer ${devToken()}` } : {}),
      ...(opts.headers || {}),
    },
    body: opts.body,
  };
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
  if (!res.ok) {
    let parsed: unknown = null;
    try { parsed = await res.json(); } catch { /* not JSON */ }
    const msg =
      (isErrorResponse(parsed) ? String(parsed.error) : null) ?? `${res.status} ${res.statusText}`;
    throw new ApiError(msg, res.status, parsed);
  }
  return res;
}
