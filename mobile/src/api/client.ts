/**
 * Typed API client for the kube-coder workspace backend (server.py).
 *
 * Auth: a Bearer token (config.token) is attached to every request. The base
 * URL is config.host, pointing at the workspace's OAuth-free ingress
 * (charts/workspace/templates/ingress-claude-api.yaml, broadened to also serve
 * /api/memory, /metrics and /health for the mobile client).
 *
 * In demo mode (config.mock) every call resolves from src/mock instead of the
 * network, so the screenshot/web build renders a full UI with no workspace.
 *
 * SSE is intentionally not used: EventSource can't send an Authorization
 * header, so screens poll getTaskOutput()/listTasks() on an interval — the
 * same fallback the dashboard SPA uses when the event stream is unavailable.
 */
import { getConfig } from '../store/config';
import {
  mockHealth,
  mockMemory,
  mockMetrics,
  mockTaskDetail,
  mockTasks,
} from '../mock/mockData';
import type { Health, MemoryRecord, Metrics, TaskDetail, TaskSummary } from './types';

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

interface ReqOpts {
  method?: string;
  body?: unknown;
  query?: Record<string, string | number | undefined>;
}

function buildUrl(host: string, path: string, query?: ReqOpts['query']): string {
  const base = host.replace(/\/+$/, '');
  let url = `${base}${path}`;
  if (query) {
    const qs = Object.entries(query)
      .filter(([, v]) => v !== undefined && v !== '')
      .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
      .join('&');
    if (qs) url += `?${qs}`;
  }
  return url;
}

async function request<T>(path: string, opts: ReqOpts = {}): Promise<T> {
  const { host, token } = getConfig();
  if (!host || !token) throw new ApiError('Not configured', 0);
  const url = buildUrl(host, path, opts.query);
  let res: Response;
  try {
    res = await fetch(url, {
      method: opts.method ?? 'GET',
      headers: {
        Accept: 'application/json',
        Authorization: `Bearer ${token}`,
        ...(opts.body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      },
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    });
  } catch (e) {
    throw new ApiError(`Network error: ${(e as Error).message}`, 0);
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

const delay = (ms: number) => new Promise((r) => setTimeout(r, ms));

// ---- Connection validation -------------------------------------------------

/** Cheap authed call used by onboarding to validate host + token. */
export async function ping(): Promise<boolean> {
  if (getConfig().mock) return true;
  await request<{ tasks?: unknown }>('/api/claude/tasks');
  return true;
}

// ---- Tasks -----------------------------------------------------------------

export async function listTasks(): Promise<TaskSummary[]> {
  if (getConfig().mock) {
    await delay(120);
    return [...mockTasks];
  }
  const data = await request<{ tasks?: TaskSummary[] } | TaskSummary[]>('/api/claude/tasks');
  return Array.isArray(data) ? data : data.tasks ?? [];
}

export async function getTask(id: string): Promise<TaskDetail> {
  if (getConfig().mock) {
    await delay(80);
    const d = mockTaskDetail(id);
    if (!d) throw new ApiError('Task not found', 404);
    return d;
  }
  return request<TaskDetail>(`/api/claude/tasks/${id}`);
}

export async function getTaskOutput(id: string, tail = 200): Promise<string> {
  if (getConfig().mock) {
    await delay(80);
    return mockTaskDetail(id)?.output ?? '';
  }
  const data = await request<{ output?: string } | string>(`/api/claude/tasks/${id}/output`, {
    query: { tail },
  });
  return typeof data === 'string' ? data : data.output ?? '';
}

export async function createTask(input: {
  prompt: string;
  workdir?: string;
  assistant?: string;
}): Promise<TaskSummary> {
  if (getConfig().mock) {
    await delay(150);
    const t: TaskSummary = {
      id: Math.random().toString(36).slice(2, 8),
      prompt: input.prompt,
      status: 'running',
      assistant: input.assistant ?? 'claude',
      workdir: input.workdir ?? '/home/dev',
      created_at: Math.floor(Date.now() / 1000),
    };
    mockTasks.unshift(t);
    return t;
  }
  return request<TaskSummary>('/api/claude/tasks', { method: 'POST', body: input });
}

export async function sendMessage(id: string, prompt: string): Promise<void> {
  if (getConfig().mock) {
    await delay(120);
    const t = mockTasks.find((x) => x.id === id);
    if (t) t.status = 'running';
    return;
  }
  await request(`/api/claude/tasks/${id}/message`, { method: 'POST', body: { prompt, submit: true } });
}

export async function killTask(id: string): Promise<void> {
  if (getConfig().mock) {
    await delay(100);
    const t = mockTasks.find((x) => x.id === id);
    if (t) t.status = 'killed';
    return;
  }
  await request(`/api/claude/tasks/${id}`, { method: 'DELETE' });
}

// ---- Memory ----------------------------------------------------------------

export async function listMemory(): Promise<MemoryRecord[]> {
  if (getConfig().mock) {
    await delay(120);
    return [...mockMemory];
  }
  const data = await request<{ entries?: MemoryRecord[] } | MemoryRecord[]>('/api/memory');
  return Array.isArray(data) ? data : data.entries ?? [];
}

// ---- Metrics / health ------------------------------------------------------

export async function getMetrics(): Promise<Metrics> {
  if (getConfig().mock) {
    await delay(100);
    return mockMetrics;
  }
  return request<Metrics>('/metrics');
}

export async function getHealth(): Promise<Health> {
  if (getConfig().mock) {
    await delay(100);
    return mockHealth;
  }
  return request<Health>('/health');
}
