import { signal, computed } from '@preact/signals';
import {
  subscribeEvents,
  eventStreamConnected,
  type DashboardEvent,
} from '../api/events';
import { listSkills, type SkillRecord } from '../api/skills';

/**
 * Skills store — mirrors store/memory.ts: signals + in-flight-deduped
 * refresh, SSE-driven updates (`skills.changed`) with a slow polling
 * safety net. Read-only in this phase, so no save/delete actions.
 */

export const skills = signal<SkillRecord[]>([]);
export const skillsLoading = signal(false);
export const skillsError = signal<string | null>(null);
export const skillsFilter = signal('');
export const skillsSystemFacet = signal<string | null>(null);
export const skillsScopeFacet = signal<string | null>(null);

export const selectedSkill = signal<SkillRecord | null>(null);

export const skillSystems = computed(() => {
  const set = new Set<string>();
  for (const s of skills.value) for (const sys of s.systems) set.add(sys);
  return [...set].sort();
});

export const skillScopes = computed(() => {
  const set = new Set<string>();
  for (const s of skills.value) set.add(s.scope);
  return [...set].sort();
});

/** Names that appear more than once = divergent variants across systems. */
export const divergentNames = computed(() => {
  const seen = new Map<string, number>();
  for (const s of skills.value) seen.set(s.name, (seen.get(s.name) ?? 0) + 1);
  return new Set([...seen.entries()].filter(([, n]) => n > 1).map(([k]) => k));
});

export const filteredSkills = computed(() => {
  const needle = skillsFilter.value.trim().toLowerCase();
  const sys = skillsSystemFacet.value;
  const scope = skillsScopeFacet.value;
  return skills.value.filter((s) => {
    if (sys && !s.systems.includes(sys)) return false;
    if (scope && s.scope !== scope) return false;
    if (!needle) return true;
    const hay = `${s.name} ${s.description} ${s.systems.join(' ')}`.toLowerCase();
    return hay.includes(needle);
  });
});

export function selectSkill(s: SkillRecord | null) {
  selectedSkill.value = s;
}

// Dedupe in-flight list fetches so the poll tick + an explicit refresh
// can't race and clobber each other (the slower response would win).
let _refreshInFlight: Promise<void> | null = null;
export async function refreshSkills(): Promise<void> {
  if (_refreshInFlight) return _refreshInFlight;
  skillsLoading.value = true;
  _refreshInFlight = (async () => {
    try {
      const r = await listSkills();
      skills.value = r.skills;
      skillsError.value = null;
      // Keep the selected record fresh: re-point it at the new list row
      // with the same identity, or clear it if the skill vanished.
      const sel = selectedSkill.value;
      if (sel) {
        const next = r.skills.find(
          (s) => s.name === sel.name && s.fingerprint === sel.fingerprint,
        ) ?? r.skills.find((s) => s.name === sel.name) ?? null;
        selectedSkill.value = next;
      }
    } catch (err) {
      skillsError.value = err instanceof Error ? err.message : String(err);
    } finally {
      skillsLoading.value = false;
      _refreshInFlight = null;
    }
  })();
  return _refreshInFlight;
}

// Real-time via the /api/events SSE stream: a `skills.changed` event
// (published by the backend SkillsSyncer when files change on disk in ANY
// harness) refreshes the list immediately. The interval is a safety net
// that polls normally when the stream is down and slows to a heartbeat
// when it's up — same discipline as store/memory.ts.
let pollHandle: ReturnType<typeof setInterval> | null = null;
let visibilityHandler: (() => void) | null = null;
let eventUnsub: (() => void) | null = null;
let eventRefreshTimer: ReturnType<typeof setTimeout> | null = null;
let lastRefreshAt = 0;
const FALLBACK_REFRESH_MS = 45000;

function doRefresh() {
  lastRefreshAt = Date.now();
  void refreshSkills();
}

function onSkillsEvent(ev: DashboardEvent) {
  if (ev.type !== 'skills.changed') return;
  if (eventRefreshTimer != null) return; // coalesce bursts
  eventRefreshTimer = setTimeout(() => {
    eventRefreshTimer = null;
    doRefresh();
  }, 250);
}

export function startSkillsPolling(intervalMs = 30000) {
  doRefresh();
  if (!eventUnsub) eventUnsub = subscribeEvents(onSkillsEvent);
  if (pollHandle) clearInterval(pollHandle);
  pollHandle = setInterval(() => {
    if (typeof document !== 'undefined' && document.hidden) return;
    if (eventStreamConnected.value && Date.now() - lastRefreshAt < FALLBACK_REFRESH_MS) return;
    doRefresh();
  }, intervalMs);
  if (typeof document !== 'undefined') {
    if (visibilityHandler) document.removeEventListener('visibilitychange', visibilityHandler);
    visibilityHandler = () => {
      if (!document.hidden) doRefresh();
    };
    document.addEventListener('visibilitychange', visibilityHandler);
  }
}

export function stopSkillsPolling() {
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
