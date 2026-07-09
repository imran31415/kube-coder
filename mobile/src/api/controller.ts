/**
 * API client for the ADMIN CONTROLLER connection — a second, independent host +
 * Bearer token (config.controllerHost / controllerToken), separate from the
 * workspace connection in client.ts. Endpoints mirror controller.py's admin
 * API: list workspaces, start/stop, and the cheap capacity summary.
 *
 * Auth: the controller admin token goes in `Authorization: Bearer`. The
 * controller's oauth2-proxy can't do a mobile OAuth handshake, so this token
 * (revealed from the web console's "Mobile access" card) is how the app reaches
 * the admin API.
 */
import { getConfig } from '../store/config';
import { mockCapacity, mockWorkspaces } from '../mock/mockData';
import { ApiError } from './client';
import type {
  ControllerCapacity,
  ControllerWorkspace,
  ControllerWorkspacesResponse,
} from './types';

const REQUEST_TIMEOUT_MS = 15000;
const delay = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function requestController<T>(
  path: string,
  opts: { method?: string; body?: unknown } = {},
): Promise<T> {
  const { controllerHost, controllerToken } = getConfig();
  if (!controllerHost || !controllerToken) throw new ApiError('Controller not configured', 0);
  const url = `${controllerHost.replace(/\/+$/, '')}${path}`;
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), REQUEST_TIMEOUT_MS);
  let res: Response;
  try {
    res = await fetch(url, {
      method: opts.method ?? 'GET',
      headers: {
        Accept: 'application/json',
        Authorization: `Bearer ${controllerToken}`,
        ...(opts.body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      },
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      signal: abort.signal,
    });
  } catch (e) {
    const aborted = (e as Error).name === 'AbortError';
    throw new ApiError(aborted ? 'Request timed out' : `Network error: ${(e as Error).message}`, 0);
  } finally {
    clearTimeout(timer);
  }
  const ctype = res.headers.get('Content-Type') || '';
  const parsed: unknown = ctype.includes('application/json')
    ? await res.json().catch(() => null)
    : await res.text();
  if (!res.ok) {
    const msg =
      parsed && typeof parsed === 'object' && 'error' in parsed
        ? String((parsed as { error: unknown }).error)
        : `${res.status} ${res.statusText}`;
    throw new ApiError(msg, res.status);
  }
  return parsed as T;
}

/** Cheap connectivity/authorization probe for the "add controller" flow. */
export async function pingController(): Promise<boolean> {
  if (getConfig().mock) {
    await delay(120);
    return true;
  }
  // /api/workspaces is the lightest authenticated GET; a 401 here means the
  // token is wrong, which surfaces as a clear error in the connect form.
  await requestController<ControllerWorkspacesResponse>('/api/workspaces');
  return true;
}

export async function listControllerWorkspaces(): Promise<ControllerWorkspace[]> {
  if (getConfig().mock) {
    await delay(150);
    return [...mockWorkspaces];
  }
  const r = await requestController<ControllerWorkspacesResponse>('/api/workspaces');
  return r.workspaces ?? [];
}

export async function getControllerCapacity(): Promise<ControllerCapacity> {
  if (getConfig().mock) {
    await delay(120);
    return mockCapacity;
  }
  return requestController<ControllerCapacity>('/api/capacity/summary');
}

export async function startWorkspace(user: string): Promise<void> {
  if (getConfig().mock) {
    await delay(200);
    return;
  }
  await requestController(`/api/workspaces/${encodeURIComponent(user)}/start`, { method: 'POST' });
}

export async function stopWorkspace(user: string): Promise<void> {
  if (getConfig().mock) {
    await delay(200);
    return;
  }
  await requestController(`/api/workspaces/${encodeURIComponent(user)}/stop`, { method: 'POST' });
}
