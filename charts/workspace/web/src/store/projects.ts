import { signal } from '@preact/signals';
import {
  subscribeEvents,
  eventStreamConnected,
  type DashboardEvent,
} from '../api/events';
import {
  listProjects,
  getProjectBrief,
  discoverProjects,
  updateProject,
  type Project,
  type ProjectBrief,
} from '../api/projects';

/**
 * State for the AI CTO page (#466): the project registry, the selected
 * project, and its deterministic brief. Mirrors store/mission.ts — an SSE
 * subscription to `project.changed` with a slow poll fallback. The server does
 * the aggregation; this only fetches and holds it.
 */

export const projects = signal<Project[]>([]);
export const projectsLoading = signal(false);
export const projectsError = signal<string | null>(null);
export const projectsLastFetch = signal<number | null>(null);

/** The selected project id, or null for the always-present **Workspace** root
 *  scope (whole /home/dev — makes an empty workspace fully usable). */
export const selectedProjectId = signal<string | null>(null);

export const brief = signal<ProjectBrief | null>(null);
export const briefLoading = signal(false);

const LAST_PROJECT_KEY = 'kc.cto.lastProject';

function rememberLastProject(id: string | null): void {
  try {
    if (id) localStorage.setItem(LAST_PROJECT_KEY, id);
    else localStorage.removeItem(LAST_PROJECT_KEY);
  } catch {
    /* private mode / SSR */
  }
}

function recallLastProject(): string | null {
  try {
    return localStorage.getItem(LAST_PROJECT_KEY);
  } catch {
    return null;
  }
}

let refreshInFlight: Promise<void> | null = null;

export async function refreshProjects(): Promise<void> {
  if (refreshInFlight) return refreshInFlight;
  projectsLoading.value = true;
  refreshInFlight = (async () => {
    try {
      projects.value = await listProjects();
      projectsError.value = null;
      projectsLastFetch.value = Date.now();
    } catch (err) {
      projectsError.value = err instanceof Error ? err.message : String(err);
    } finally {
      projectsLoading.value = false;
      refreshInFlight = null;
    }
  })();
  return refreshInFlight;
}

/** Load the brief for a project id (skips the fetch for the Workspace scope,
 *  which has no registry record). */
export async function loadBrief(id: string | null): Promise<void> {
  if (!id) {
    brief.value = null;
    return;
  }
  briefLoading.value = true;
  try {
    const b = await getProjectBrief(id);
    // Guard against a late load landing after the user switched projects.
    if (selectedProjectId.value === id) brief.value = b;
  } catch {
    if (selectedProjectId.value === id) brief.value = null;
  } finally {
    briefLoading.value = false;
  }
}

/** Select a project (or the Workspace scope when null): loads its brief and
 *  stamps last_seen_at so the returning-visit delta strip has a baseline. */
export async function selectProject(id: string | null): Promise<void> {
  if (selectedProjectId.value === id && (id === null || brief.value)) return;
  selectedProjectId.value = id;
  rememberLastProject(id);
  brief.value = null;
  await loadBrief(id);
  if (id) void stampLastSeen(id);
}

/** Previous last_seen_at per project, captured the moment we select it — the
 *  baseline the delta strip diffs the brief against (before we overwrite it
 *  server-side). */
export const lastSeenBaseline = signal<Record<string, number | null>>({});

async function stampLastSeen(id: string): Promise<void> {
  const prev = projects.value.find((p) => p.id === id)?.last_seen_at ?? null;
  lastSeenBaseline.value = { ...lastSeenBaseline.value, [id]: prev };
  try {
    await updateProject(id, { last_seen_at: Date.now() / 1000 });
  } catch {
    /* best-effort — a failed stamp just means no delta baseline advance */
  }
}

/** The most-active project (highest pulse.last_activity_at), or the first. */
export function mostActive(list: Project[]): Project | null {
  if (list.length === 0) return null;
  return [...list].sort(
    (a, b) => (b.pulse?.last_activity_at ?? 0) - (a.pulse?.last_activity_at ?? 0),
  )[0];
}

let discovered = false;

/** First-visit bootstrap: zero-touch discovery (server auto-provisions
 *  confident candidates), then load the registry and auto-select the last-seen
 *  project, else the most active. Safe to call on every mount — discovery runs
 *  once per page load. */
export async function initCto(): Promise<void> {
  if (!discovered) {
    discovered = true;
    try {
      await discoverProjects(true);
    } catch {
      /* discovery is best-effort; the registry may already be populated */
    }
  }
  await refreshProjects();
  if (selectedProjectId.value !== null && brief.value) return; // already chosen
  const list = projects.value;
  const remembered = recallLastProject();
  const pick =
    (remembered && list.find((p) => p.id === remembered)) || mostActive(list);
  await selectProject(pick ? pick.id : null);
}

/** Archive a wrongly-discovered project from the card overflow (never an
 *  upfront confirm wizard — UX F1). Non-destructive: status → archived. */
export async function archiveProject(id: string): Promise<void> {
  try {
    await updateProject(id, { status: 'archived' });
  } catch {
    /* best-effort */
  }
  if (selectedProjectId.value === id) await selectProject(null);
  await refreshProjects();
}

// ── real-time (project.changed SSE with poll fallback, mission.ts pattern) ──

let pollHandle: ReturnType<typeof setInterval> | null = null;
let visibilityHandler: (() => void) | null = null;
let eventUnsub: (() => void) | null = null;
let eventRefreshTimer: ReturnType<typeof setTimeout> | null = null;
const FALLBACK_REFRESH_MS = 45000;

function onDashboardEvent(ev: DashboardEvent): void {
  // Projects change on registry writes; their pulse changes when tasks move,
  // so refresh on both project.changed and task.status.
  if (ev.type !== 'projects.changed' && ev.type !== 'task.status') return;
  if (eventRefreshTimer != null) return;
  eventRefreshTimer = setTimeout(() => {
    eventRefreshTimer = null;
    void refreshProjects();
    // Keep the open brief current too (its task counts move with task.status).
    if (selectedProjectId.value) void loadBrief(selectedProjectId.value);
  }, 250);
}

export function startProjectsPolling(intervalMs = 12000): void {
  if (!eventUnsub) eventUnsub = subscribeEvents(onDashboardEvent);
  if (pollHandle) clearInterval(pollHandle);
  pollHandle = setInterval(() => {
    if (typeof document !== 'undefined' && document.hidden) return;
    if (
      eventStreamConnected.value &&
      Date.now() - (projectsLastFetch.value || 0) < FALLBACK_REFRESH_MS
    ) {
      return;
    }
    void refreshProjects();
  }, intervalMs);
  if (typeof document !== 'undefined') {
    if (visibilityHandler) document.removeEventListener('visibilitychange', visibilityHandler);
    visibilityHandler = () => {
      if (!document.hidden) void refreshProjects();
    };
    document.addEventListener('visibilitychange', visibilityHandler);
  }
}

export function stopProjectsPolling(): void {
  if (pollHandle) clearInterval(pollHandle);
  pollHandle = null;
  if (eventUnsub) {
    eventUnsub();
    eventUnsub = null;
  }
  if (eventRefreshTimer != null) {
    clearTimeout(eventRefreshTimer);
    eventRefreshTimer = null;
  }
  if (visibilityHandler && typeof document !== 'undefined') {
    document.removeEventListener('visibilitychange', visibilityHandler);
    visibilityHandler = null;
  }
}

/** Reset for tests. */
export function _resetProjectsForTest(): void {
  projects.value = [];
  brief.value = null;
  selectedProjectId.value = null;
  projectsLastFetch.value = null;
  discovered = false;
  stopProjectsPolling();
}
