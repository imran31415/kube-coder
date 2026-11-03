/**
 * The workspace transport: `fetch` on the phone and in a browser, the desktop
 * core in the desktop app (#833).
 *
 * The desktop webview has no network access of its own — its CSP allows only
 * IPC — so a workspace request is handed to the core (`kc_request`) as a path.
 * The core attaches the token from the OS keychain, builds the URL from the
 * connected host only and never follows a redirect (desktop/src-tauri/src/
 * http.rs). So here the origin is stripped and must match the connected host,
 * and the Authorization header the client added is dropped: the webview's
 * copy of the token is only a mask.
 *
 * Pure apart from the injected `invoke`, so the node-side vitest covers it.
 */

/** The slice of `Response` the API client reads. */
export interface ResponseLike {
  ok: boolean;
  status: number;
  statusText: string;
  type: string;
  headers: { get(name: string): string | null };
  json(): Promise<unknown>;
  text(): Promise<string>;
}

export interface WorkspaceInit {
  method?: string;
  headers?: Record<string, string>;
  body?: string | Blob;
  redirect?: 'manual';
  signal?: AbortSignal;
}

interface WsResponse {
  status: number;
  statusText: string;
  contentType: string;
  body: string;
}

type Invoke = <T>(cmd: string, args?: Record<string, unknown>) => Promise<T>;

/** `url` minus the connected host, or null when it is not on that host. */
export function pathOnHost(host: string, url: string): string | null {
  const base = (host || '').replace(/\/+$/, '');
  if (!base || !url.startsWith(base)) return null;
  // `https://me.example.com.evil.com/` also starts with the host's string; the
  // next character is what tells them apart.
  const rest = url.slice(base.length);
  return rest.startsWith('/') ? rest : null;
}

function base64(bytes: Uint8Array): string {
  let bin = '';
  const step = 0x8000;
  for (let i = 0; i < bytes.length; i += step) {
    bin += String.fromCharCode(...bytes.subarray(i, i + step));
  }
  return btoa(bin);
}

function abortError(): Error {
  const e = new Error('Aborted');
  e.name = 'AbortError';
  return e;
}

/** A `fetch` for workspace URLs that runs through the desktop core. */
export function desktopFetch(invoke: Invoke, getHost: () => string) {
  return async (url: string, init: WorkspaceInit = {}): Promise<ResponseLike> => {
    const path = pathOnHost(getHost(), url);
    if (path === null) throw new TypeError('Refusing a request to a host other than the connected workspace');
    const headers: Record<string, string> = {};
    for (const [k, v] of Object.entries(init.headers ?? {})) {
      if (k.toLowerCase() !== 'authorization') headers[k] = v;
    }
    const req: Record<string, unknown> = { method: init.method ?? 'GET', path, headers };
    if (typeof init.body === 'string') req.body = init.body;
    else if (init.body) req.bodyBase64 = base64(new Uint8Array(await init.body.arrayBuffer()));
    if (init.signal?.aborted) throw abortError();
    const call = invoke<WsResponse>('kc_request', { req });
    const res = await (init.signal
      ? Promise.race([
          call,
          new Promise<never>((_, reject) => init.signal!.addEventListener('abort', () => reject(abortError()))),
        ])
      : call);
    return {
      ok: res.status >= 200 && res.status < 300,
      status: res.status,
      statusText: res.statusText,
      type: 'basic',
      headers: { get: (n) => (n.toLowerCase() === 'content-type' ? res.contentType : null) },
      json: async () => JSON.parse(res.body),
      text: async () => res.body,
    };
  };
}
