/**
 * Typed fetch wrapper with read deadlines and cancellation. Preserves the
 * dashboard's auth behavior (charts/workspace/web): prepend /oauth to /api/*
 * (so the oauth2 cookie attaches), and on a 401 or the "auth expired" symptom
 * bounce the page to /oauth2/start.
 *
 * For local dev a Bearer token from localStorage['kc.devToken'] is attached
 * when present (matches controller.py's CONTROLLER_DEV_TOKEN).
 */

export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;
  readonly code?: string;
  constructor(message: string, status: number, body: unknown, code?: string) {
    super(message);
    this.status = status;
    this.body = body;
    this.code = code;
  }
}

interface Options extends Omit<RequestInit, 'body'> {
  body?: unknown;
  timeoutMs?: number;
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
  // No status code reaches us — only a generic "Failed to fetch".
  if (!(e instanceof TypeError)) return false;
  const msg = (e.message || '').toLowerCase();
  return msg.includes('failed to fetch') || msg.includes('networkerror') || msg.includes('load failed');
}

export async function api<T = unknown>(path: string, opts: Options = {}): Promise<T> {
  const { body, query, headers, signal, timeoutMs, ...rest } = opts;
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  let rejectAbort: (reason: unknown) => void = () => {};
  const interrupted = new Promise<never>((_, reject) => { rejectAbort = reject; });
  const abort = () => {
    controller.abort();
    rejectAbort(new DOMException('Request cancelled', 'AbortError'));
  };
  signal?.addEventListener('abort', abort, { once: true });
  const url = buildUrl(path, query);
  async function request(): Promise<T> {
    const token = devToken();
    const res = await fetch(url, {
      ...rest, signal: controller.signal,
      headers: { Accept: 'application/json',
        ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(headers as Record<string, string> | undefined) },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (res.status === 401 && url.startsWith('/oauth/')) {
      redirectToSignIn();
      throw new ApiError('Session expired. Redirecting to sign in.', 401, null);
    }
    const isJson = (res.headers.get('Content-Type') || '').includes('application/json');
    // An empty or malformed JSON body must not turn a real status (502, 504…)
    // into a network error: parse tolerantly and keep res.status.
    const parsed: unknown = isJson ? await res.json().catch(() => null) : await res.text();
    if (!res.ok) {
      const data = parsed && typeof parsed === 'object' ? parsed as Record<string, unknown> : null;
      throw new ApiError(typeof data?.error === 'string' ? data.error : `Request failed (${res.status})`, res.status, parsed);
    }
    if (!isJson) throw new ApiError('The server returned an unexpected response.', res.status, null);
    return parsed as T;
  }
  try {
    if (signal?.aborted) throw new DOMException('Request cancelled', 'AbortError');
    // Reads have a deadline; mutation outcomes must never be guessed or replayed.
    const limit = timeoutMs ?? ((opts.method ?? 'GET') === 'GET' ? 35000 : 0);
    if (limit > 0) timer = setTimeout(() => {
      rejectAbort(new ApiError('The request took too long. Try again.', 0, null, 'request_timeout'));
      controller.abort();
    }, limit);
    return await Promise.race([request(), interrupted]);
  } catch (error) {
    if (error instanceof ApiError || (error instanceof DOMException && error.name === 'AbortError')) throw error;
    // An expired oauth2 session surfaces as this TypeError, not a 401: the
    // proxy's 302 to the identity provider is cross-origin, so fetch refuses
    // it. Send the user to sign in, as the dashboard does (once per page load).
    if (isAuthExpiredError(error) && url.startsWith('/oauth/')) {
      redirectToSignIn();
      throw new ApiError('Session expired. Redirecting to sign in.', 401, null);
    }
    throw new ApiError("Can't reach the server.", 0, null, 'network_error');
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', abort);
  }
}

export const apiGet = <T>(path: string, query?: Options['query'], signal?: AbortSignal, timeoutMs?: number) =>
  api<T>(path, { method: 'GET', query, signal, timeoutMs });
export const apiPost = <T>(path: string, body?: unknown) => api<T>(path, { method: 'POST', body });
