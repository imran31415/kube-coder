import { signal, computed } from '@preact/signals';
import {
  subscribeEvents,
  eventStreamConnected,
  type DashboardEvent,
} from '../api/events';
import {
  listTriggers,
  fireCron,
  suspendCron,
  resumeCron,
  deleteCron,
  testWebhook,
  deleteWebhook,
  checkPageWatch,
  suspendPageWatch,
  resumePageWatch,
  deletePageWatch,
  type Trigger,
} from '../api/triggers';
import { pushToast } from './ui';

export const triggers = signal<Trigger[]>([]);
export const triggersLoading = signal(false);
export const triggersError = signal<string | null>(null);
export const triggerFilter = signal('');

export const filteredTriggers = computed(() => {
  const needle = triggerFilter.value.trim().toLowerCase();
  if (!needle) return triggers.value;
  return triggers.value.filter((t) =>
    `${t.kind} ${t.id} ${t.prompt} ${t.schedule ?? ''} ${t.workdir ?? ''} ${t.url ?? ''} ${t.selector ?? ''}`
      .toLowerCase()
      .includes(needle),
  );
});

export async function refreshTriggers(): Promise<void> {
  triggersLoading.value = true;
  try {
    triggers.value = await listTriggers();
    triggersError.value = null;
  } catch (err) {
    triggersError.value = err instanceof Error ? err.message : String(err);
  } finally {
    triggersLoading.value = false;
  }
}

export async function fire(t: Trigger): Promise<void> {
  try {
    if (t.kind === 'cron') {
      await fireCron(t.id);
      pushToast(`Fired ${t.id}`, { kind: 'success' });
    } else if (t.kind === 'page-watch') {
      // A page-watch check is conditional, so report what actually happened
      // rather than claiming it fired. "Checked — no change" is the common
      // and correct answer, and saying "Fired" would be a lie.
      const res = await checkPageWatch(t.id);
      const said: Record<string, string> = {
        changed: 'Page changed — task started',
        unchanged: 'Checked — no change',
        baseline: 'Baseline recorded — future changes will fire',
        error: res.error ? `Could not read the page: ${res.error}` : 'Could not read the page',
        // The scheduled check got there first. Saying nothing would look like
        // the button did nothing, and claiming "no change" would be a guess.
        busy: 'A check is already running - the result will appear in a moment',
      };
      pushToast(said[res.outcome] ?? `Checked ${t.id}`, {
        kind: res.outcome === 'error' ? 'danger' : res.outcome === 'changed' ? 'success' : 'info',
      });
    } else {
      await testWebhook(t.id);
      pushToast(`Fired ${t.id}`, { kind: 'success' });
    }
    await refreshTriggers();
  } catch (err) {
    pushToast(err instanceof Error ? err.message : 'Fire failed', { kind: 'danger' });
  }
}

export async function toggleSuspend(t: Trigger): Promise<void> {
  // Webhooks have no timer to pause; both scheduled kinds do.
  if (t.kind !== 'cron' && t.kind !== 'page-watch') return;
  try {
    if (t.kind === 'page-watch') {
      if (t.suspended) await resumePageWatch(t.id);
      else await suspendPageWatch(t.id);
    } else if (t.suspended) await resumeCron(t.id);
    else await suspendCron(t.id);
    pushToast(t.suspended ? 'Resumed' : 'Paused', { kind: 'info' });
    await refreshTriggers();
  } catch (err) {
    pushToast(err instanceof Error ? err.message : 'Toggle failed', { kind: 'danger' });
  }
}

export async function removeTrigger(t: Trigger): Promise<void> {
  try {
    if (t.kind === 'cron') await deleteCron(t.id);
    else if (t.kind === 'page-watch') await deletePageWatch(t.id);
    else await deleteWebhook(t.id);
    pushToast(`Deleted ${t.id}`, { kind: 'warn' });
    await refreshTriggers();
  } catch (err) {
    pushToast(err instanceof Error ? err.message : 'Delete failed', { kind: 'danger' });
  }
}

// Real-time via the /api/events SSE stream (issue #93): a `trigger.fired`
// event (webhook received / cron fired) refreshes the list immediately, and
// the interval below is kept only as a safety net — it polls normally when the
// stream is down and slows to a heartbeat when it's up.
let pollHandle: ReturnType<typeof setInterval> | null = null;
let eventUnsub: (() => void) | null = null;
let eventRefreshTimer: ReturnType<typeof setTimeout> | null = null;
let lastRefreshAt = 0;
const FALLBACK_REFRESH_MS = 45000;

function doRefresh() {
  lastRefreshAt = Date.now();
  void refreshTriggers();
}

function onTriggerEvent(ev: DashboardEvent) {
  if (ev.type !== 'trigger.fired') return;
  if (eventRefreshTimer != null) return; // coalesce bursts
  eventRefreshTimer = setTimeout(() => {
    eventRefreshTimer = null;
    doRefresh();
  }, 250);
}

export function startTriggerPolling(intervalMs = 30000) {
  doRefresh();
  if (!eventUnsub) eventUnsub = subscribeEvents(onTriggerEvent);
  if (pollHandle) clearInterval(pollHandle);
  pollHandle = setInterval(() => {
    if (eventStreamConnected.value && Date.now() - lastRefreshAt < FALLBACK_REFRESH_MS) return;
    doRefresh();
  }, intervalMs);
}
export function stopTriggerPolling() {
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
}
