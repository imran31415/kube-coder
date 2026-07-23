import { signal, computed } from '@preact/signals';
import {
  subscribeEvents,
  eventStreamConnected,
  type DashboardEvent,
} from '../api/events';
import {
  getMissionQueue,
  type MissionCard,
  type MissionKind,
  type MissionPulse,
} from '../api/mission';

export const missionCards = signal<MissionCard[]>([]);
export const missionPulse = signal<MissionPulse | null>(null);
export const missionLoading = signal(false);
export const missionError = signal<string | null>(null);
export const missionLastFetch = signal<number | null>(null);

/** Kind chip filter: a single kind, or 'all'. */
export type MissionKindFilter = MissionKind | 'all';
export const missionKindFilter = signal<MissionKindFilter>('all');

/** Free-text filter over repo / branch / title / headline. */
export const missionFilter = signal('');

export const filteredMissionCards = computed(() => {
  const needle = missionFilter.value.trim().toLowerCase();
  let list = missionCards.value;
  if (missionKindFilter.value !== 'all') {
    list = list.filter((c) => c.kind === missionKindFilter.value);
  }
  if (needle) {
    list = list.filter((c) => {
      const hay = `${c.repo} ${c.branch} ${c.title} ${c.headline}`.toLowerCase();
      return hay.includes(needle);
    });
  }
  return list;
});

let inFlight: Promise<void> | null = null;

export async function refreshMission(): Promise<void> {
  if (inFlight) return inFlight;
  missionLoading.value = true;
  inFlight = (async () => {
    try {
      const q = await getMissionQueue();
      missionCards.value = q.cards;
      missionPulse.value = q.pulse;
      missionError.value = null;
      missionLastFetch.value = Date.now();
    } catch (err) {
      missionError.value = err instanceof Error ? err.message : String(err);
    } finally {
      missionLoading.value = false;
      inFlight = null;
    }
  })();
  return inFlight;
}

// Real-time updates via the /api/events SSE stream (issue #93), with the
// interval below kept as a safety-net fallback — same hybrid as store/tasks.
// task.created / task.status cover builds and sub-agents; chats have no
// dedicated event yet, so the timer keeps their column fresh. A burst of
// events is coalesced into one refresh.
let pollHandle: ReturnType<typeof setInterval> | null = null;
let visibilityHandler: (() => void) | null = null;
let eventUnsub: (() => void) | null = null;
let eventRefreshTimer: ReturnType<typeof setTimeout> | null = null;
// Once connected, the timer only refreshes if we haven't fetched in this long.
const FALLBACK_REFRESH_MS = 45000;

function onDashboardEvent(ev: DashboardEvent) {
  if (ev.type !== 'task.created' && ev.type !== 'task.status') return;
  // Coalesce bursts (e.g. several tasks finishing at once) into one refresh.
  if (eventRefreshTimer != null) return;
  eventRefreshTimer = setTimeout(() => {
    eventRefreshTimer = null;
    void refreshMission();
  }, 200);
}

export function startMissionPolling(intervalMs = 10000) {
  refreshMission();
  if (!eventUnsub) eventUnsub = subscribeEvents(onDashboardEvent);
  if (pollHandle) clearInterval(pollHandle);
  // Skip ticks while the tab is hidden — mobile users especially shouldn't
  // pay the network/battery cost for refreshes they aren't looking at. On
  // visibilitychange back to visible, fire one immediate refresh so the UI
  // is current the instant they look at it again, then resume the normal
  // interval.
  pollHandle = setInterval(() => {
    if (typeof document !== 'undefined' && document.hidden) return;
    // When SSE is live, events do the work — only refresh as a slow safety net.
    if (
      eventStreamConnected.value &&
      Date.now() - (missionLastFetch.value || 0) < FALLBACK_REFRESH_MS
    ) {
      return;
    }
    void refreshMission();
  }, intervalMs);
  if (typeof document !== 'undefined') {
    if (visibilityHandler) document.removeEventListener('visibilitychange', visibilityHandler);
    visibilityHandler = () => {
      if (!document.hidden) void refreshMission();
    };
    document.addEventListener('visibilitychange', visibilityHandler);
  }
}

export function stopMissionPolling() {
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
