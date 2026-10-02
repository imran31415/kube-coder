import { signal, effect } from '@preact/signals';

export type Theme = 'system' | 'dark' | 'light';
export type Density = 'comfortable' | 'compact';

interface PersistedPrefs {
  theme: Theme;
  density: Density;
  railCollapsed: boolean;
  masterCollapsed: boolean;
}

const STORAGE_KEY = 'kube-coder.ui';

function loadPrefs(): PersistedPrefs {
  const fallback: PersistedPrefs = {
    theme: 'system',
    density: 'comfortable',
    // Default-expanded rail so the dashboard opens with readable text labels
    // (Desktop, Build, Memory, …) next to each icon — new users shouldn't
    // have to hover for tooltips or discover the toggle to learn what the
    // nav does. The chevron at the bottom of the rail still collapses it to
    // an icon-only strip in one click, and that choice persists for users
    // who prefer the compact rail.
    railCollapsed: false,
    masterCollapsed: false,
  };
  if (typeof localStorage === 'undefined') return fallback;
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return fallback;
    const parsed = JSON.parse(raw) as Partial<PersistedPrefs>;
    return {
      theme: parsed.theme === 'dark' || parsed.theme === 'light' ? parsed.theme : 'system',
      density: parsed.density === 'compact' ? 'compact' : 'comfortable',
      railCollapsed: parsed.railCollapsed === true,
      masterCollapsed: parsed.masterCollapsed === true,
    };
  } catch {
    return fallback;
  }
}

const initial = loadPrefs();

export const theme = signal<Theme>(initial.theme);
export const density = signal<Density>(initial.density);

/** Collapses the left navigation rail to an icon-only strip on desktop. */
export const railCollapsed = signal<boolean>(initial.railCollapsed);

// Per-group disclosure state for the categorized rail (#267). Stored under
// its own versioned key (not kube-coder.ui) so the group schema can evolve
// without migrating the main prefs blob. Holds *collapsed* group ids.
const RAIL_GROUPS_KEY = 'kc.rail.groups.v1';

/**
 * Groups that start collapsed in a browser that has never set a preference.
 *
 * The rail then rests at five rows — Mission Control and its agent surfaces —
 * instead of thirteen. Workspace and Knowledge are destinations you go to
 * deliberately, not ones you scan, so they cost a click rather than permanent
 * vertical space. Every route still exists and every deep link still resolves;
 * this is disclosure state only.
 */
export const DEFAULT_COLLAPSED_GROUPS = ['workspace', 'knowledge'];

export function loadCollapsedGroups(): string[] {
  if (typeof localStorage === 'undefined') return [...DEFAULT_COLLAPSED_GROUPS];
  try {
    const raw = localStorage.getItem(RAIL_GROUPS_KEY);
    // Distinguish "never set" from "set to empty". A user who deliberately
    // expanded both groups has a stored `[]`, and must keep it — only a
    // browser with no stored key inherits the new default.
    if (raw === null) return [...DEFAULT_COLLAPSED_GROUPS];
    const parsed = JSON.parse(raw) as unknown;
    return Array.isArray(parsed)
      ? parsed.filter((v): v is string => typeof v === 'string')
      : [...DEFAULT_COLLAPSED_GROUPS];
  } catch {
    return [...DEFAULT_COLLAPSED_GROUPS];
  }
}

/** Ids of rail nav groups the user has collapsed. Persisted. */
export const collapsedRailGroups = signal<string[]>(loadCollapsedGroups());

/**
 * The group auto-revealed because the active route lives inside it.
 *
 * Deliberately NOT persisted and deliberately separate from
 * `collapsedRailGroups`: navigating to /memory should show you where you are,
 * but it should not silently re-expand Knowledge forever. Otherwise the
 * five-row default erodes to thirteen within a session or two and this whole
 * change delivers nothing durable. Only an explicit chevron click — which goes
 * through `toggleRailGroup` — is remembered.
 */
export const autoExpandedGroup = signal<string | null>(null);

/** True when `id` should render expanded: not collapsed, or transiently open. */
export function isRailGroupExpanded(id: string): boolean {
  return !collapsedRailGroups.value.includes(id) || autoExpandedGroup.value === id;
}

export function toggleRailGroup(id: string) {
  const cur = collapsedRailGroups.value;
  // An explicit toggle is the user's real preference, so it also clears the
  // transient reveal — otherwise collapsing the group you're standing in
  // would appear to do nothing.
  if (autoExpandedGroup.value === id) autoExpandedGroup.value = null;
  collapsedRailGroups.value = cur.includes(id) ? cur.filter((g) => g !== id) : [...cur, id];
}

/**
 * Transiently reveal the group containing the active route (never persisted).
 * Pass null when the active route belongs to no group, so the previous
 * reveal is cleared rather than left standing.
 */
export function expandRailGroup(id: string | null) {
  if (autoExpandedGroup.value !== id) autoExpandedGroup.value = id;
}

if (typeof localStorage !== 'undefined') {
  effect(() => {
    try {
      localStorage.setItem(RAIL_GROUPS_KEY, JSON.stringify(collapsedRailGroups.value));
    } catch {
      // localStorage may be unavailable; skip persistence.
    }
  });
}
/** Hides the master task list so the detail pane takes the full width. */
export const masterCollapsed = signal<boolean>(initial.masterCollapsed);
/** Transient — Preview tab full-screen mode. Not persisted; resets on reload. */
export const previewFullscreen = signal<boolean>(false);

// Overlay state — only one of {drawer, sheet, palette} should be visible at a time.
export const drawerOpen = signal<DrawerKey | null>(null);
export const sheetOpen = signal<SheetKey | null>(null);
export const paletteOpen = signal(false);

export type DrawerKey = 'settings' | 'files' | 'github' | 'metrics' | 'new-task' | 'memory-edit' | 'trigger-edit' | 'desktop-edit';
export type SheetKey = 'task-detail' | 'memory-detail' | 'skill-detail' | 'trigger-detail' | 'new-task' | 'more';

export interface Toast {
  id: string;
  message: string;
  kind: 'info' | 'success' | 'warn' | 'danger';
  /** ms; 0 = sticky */
  ttl: number;
}
export const toasts = signal<Toast[]>([]);

let toastSeq = 0;
export function pushToast(message: string, opts: Partial<Omit<Toast, 'id' | 'message'>> = {}) {
  const id = `t${++toastSeq}`;
  const toast: Toast = {
    id,
    message,
    kind: opts.kind ?? 'info',
    ttl: opts.ttl ?? 3500,
  };
  toasts.value = [...toasts.value, toast];
  if (toast.ttl > 0) {
    setTimeout(() => dismissToast(id), toast.ttl);
  }
  return id;
}

export function dismissToast(id: string) {
  toasts.value = toasts.value.filter((t) => t.id !== id);
}

// Persistence — single effect saves all persisted fields when any change.
if (typeof localStorage !== 'undefined') {
  effect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        theme: theme.value,
        density: density.value,
        railCollapsed: railCollapsed.value,
        masterCollapsed: masterCollapsed.value,
      }));
    } catch {
      // localStorage may be unavailable (Safari private mode, quota); silently skip.
    }
  });
}

// Apply theme + density to <html> so CSS can react via attribute selectors.
export function applyDocumentAttrs(themeValue: Theme, densityValue: Density) {
  const html = document.documentElement;
  if (themeValue === 'system') {
    html.removeAttribute('data-theme');
  } else {
    html.setAttribute('data-theme', themeValue);
  }
  html.setAttribute('data-density', densityValue);
}

if (typeof document !== 'undefined') {
  effect(() => {
    applyDocumentAttrs(theme.value, density.value);
  });
}
