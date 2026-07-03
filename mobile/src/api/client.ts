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
  mockApps,
  mockHealth,
  mockMemory,
  mockMetrics,
  mockTaskDetail,
  mockTasks,
} from '../mock/mockData';
import type { AppEntry, Health, MemoryRecord, Metrics, TaskDetail, TaskSummary } from './types';

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

// A stalled connection should fail visibly, not spin forever — polling screens
// recover on the next tick, and one-shot actions surface a real error.
const REQUEST_TIMEOUT_MS = 15000;

async function request<T>(path: string, opts: ReqOpts = {}): Promise<T> {
  const { host, token } = getConfig();
  if (!host || !token) throw new ApiError('Not configured', 0);
  const url = buildUrl(host, path, opts.query);
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), REQUEST_TIMEOUT_MS);
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

const delay = (ms: number) => new Promise((r) => setTimeout(r, ms));

// ---- Connection validation -------------------------------------------------

/** Cheap authed call used by onboarding to validate host + token. */
export async function ping(): Promise<boolean> {
  if (getConfig().mock) return true;
  await request<{ tasks?: unknown }>('/api/claude/tasks');
  return true;
}

// ---- Tasks -----------------------------------------------------------------

// The server (and the dashboard SPA) name the task id `task_id`; the app uses
// `id` internally. Adapt the server shape here so the screens + mock data stay
// on one consistent shape. Without this, `id` is undefined → list keys break and
// the detail fetch hits /api/claude/tasks/ (empty id) → 404 → stuck loading.
//
// Status is also normalized to the app's canonical set: the server emits
// `waiting-for-input` / `completed`, but the status pill + the "active"
// (can-send-a-message) checks expect `waiting` / `done`.
function normStatus(s: string | undefined): string {
  if (s === 'waiting-for-input' || s === 'waiting_for_input' || s === 'waiting_input') return 'waiting';
  if (s === 'completed') return 'done';
  return s ?? '';
}

function withId<T extends { id?: string; task_id?: string; status?: string }>(
  t: T,
): T & { id: string; status: string } {
  return { ...t, id: t.task_id ?? t.id ?? '', status: normStatus(t.status) };
}

export async function listTasks(): Promise<TaskSummary[]> {
  if (getConfig().mock) {
    await delay(120);
    return [...mockTasks];
  }
  const data = await request<{ tasks?: TaskSummary[] } | TaskSummary[]>('/api/claude/tasks');
  const list = Array.isArray(data) ? data : data.tasks ?? [];
  return list.map(withId);
}

export async function getTask(id: string): Promise<TaskDetail> {
  if (getConfig().mock) {
    await delay(80);
    const d = mockTaskDetail(id);
    if (!d) throw new ApiError('Task not found', 404);
    return d;
  }
  return withId(await request<TaskDetail>(`/api/claude/tasks/${id}`));
}

export async function getTaskOutput(id: string, tail = 200): Promise<string> {
  if (getConfig().mock) {
    await delay(80);
    return mockTaskDetail(id)?.output ?? '';
  }
  // ansi=1 keeps the SGR color escapes; the detail view parses + cleans them
  // (util/ansi → parseAnsiLines: colored spans, divider rules + blank runs dropped).
  const data = await request<{ output?: string } | string>(`/api/claude/tasks/${id}/output`, {
    query: { tail, ansi: 1 },
  });
  return typeof data === 'string' ? data : data.output ?? '';
}

/** Send one control key (shift-tab, escape, up, down, enter, ctrl-c, …) to the
 * live session — for the mobile key bar (no physical keyboard). */
export async function sendKey(id: string, key: string): Promise<void> {
  if (getConfig().mock) {
    await delay(60);
    return;
  }
  await request(`/api/claude/tasks/${id}/key`, { method: 'POST', body: { key } });
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
  return withId(await request<TaskSummary>('/api/claude/tasks', { method: 'POST', body: input }));
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
  // The server wraps records under `memories`; accept `entries` too defensively.
  const data = await request<{ memories?: RawMemory[]; entries?: RawMemory[] } | RawMemory[]>(
    '/api/memory',
  );
  const recs = Array.isArray(data) ? data : data.memories ?? data.entries ?? [];
  return recs.map(normalizeMemory);
}

// The server sends `tags` as a comma-joined string and the real array under
// `tags_list`; the app wants `tags: string[]`. Normalize so MemoryScreen's
// `tags.map()` doesn't crash on a string ("undefined is not a function").
type RawMemory = Omit<MemoryRecord, 'tags'> & { tags?: unknown; tags_list?: string[] };

function normalizeMemory(m: RawMemory): MemoryRecord {
  const tags = Array.isArray(m.tags_list)
    ? m.tags_list
    : Array.isArray(m.tags)
      ? (m.tags as string[])
      : typeof m.tags === 'string' && m.tags.trim()
        ? m.tags.split(',').map((s) => s.trim()).filter(Boolean)
        : [];
  return { ...m, tags };
}

// ---- Applications ------------------------------------------------------------

/** Apps running in the workspace (auto-discovered listeners + pins). */
export async function listApps(): Promise<AppEntry[]> {
  if (getConfig().mock) {
    await delay(120);
    return [...mockApps];
  }
  const data = await request<{ apps?: AppEntry[] }>('/api/apps');
  return data.apps ?? [];
}

/**
 * URL + headers for embedding an app in the WebView.
 *
 * A WebView only attaches headers to its FIRST request, so we point it at the
 * Bearer-authenticated session bootstrap (/api/claude/apps/session): the server
 * validates the token, sets a short-lived app-session cookie scoped to the app
 * proxy, and 302s into /api/app-proxy/<port>/. Every sub-resource the embedded
 * app loads after that authenticates via the cookie the WebView just stored.
 */
export function appEmbedSource(port: number): { uri: string; headers: Record<string, string> } {
  const { host, token } = getConfig();
  const next = encodeURIComponent(`/api/app-proxy/${port}/`);
  return {
    uri: `${host.replace(/\/+$/, '')}/api/claude/apps/session?next=${next}`,
    headers: { Authorization: `Bearer ${token}` },
  };
}

/** Plain proxy URL for an app — for "open in browser". */
export function appProxyUrl(port: number): string {
  return `${getConfig().host.replace(/\/+$/, '')}/api/app-proxy/${port}/`;
}

// ---- Metrics / health ------------------------------------------------------

// server.py /metrics is nested ({cpu:{usage_percent}, memory:{used_mb,total_mb},
// disk:{used_gb,total_gb}}); the app + mock use a flat shape. Flatten here.
interface RawMetrics {
  cpu?: { usage_percent?: number };
  memory?: { used_mb?: number; total_mb?: number };
  disk?: { used_gb?: number; total_gb?: number };
}

export async function getMetrics(): Promise<Metrics> {
  if (getConfig().mock) {
    await delay(100);
    return mockMetrics;
  }
  const d = await request<RawMetrics>('/metrics');
  return {
    cpu_percent: d.cpu?.usage_percent ?? 0,
    memory_used_mb: d.memory?.used_mb ?? 0,
    memory_total_mb: d.memory?.total_mb ?? 0,
    disk_used_gb: d.disk?.used_gb ?? 0,
    disk_total_gb: d.disk?.total_gb ?? 0,
  };
}

// server.py /health is { status, services: { vscode, terminal, browser } }; the
// app wants flat booleans. Map services → flat + derive `ok` from status.
interface RawHealth {
  status?: string;
  services?: { vscode?: boolean; terminal?: boolean; browser?: boolean };
}

export async function getHealth(): Promise<Health> {
  if (getConfig().mock) {
    await delay(100);
    return mockHealth;
  }
  const d = await request<RawHealth>('/health');
  return {
    vscode: d.services?.vscode,
    terminal: d.services?.terminal,
    browser: d.services?.browser,
    ok: d.status === 'healthy',
  };
}
