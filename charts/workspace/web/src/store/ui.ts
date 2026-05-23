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
    // Default-collapsed rail so the dashboard opens with the most chrome
    // hidden — reduces visual noise on first paint, and the toggle at the
    // bottom of the rail (chevron) is a one-click expand. Users who prefer
    // the labeled rail can pin it open and the choice persists.
    railCollapsed: true,
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
/** Hides the master task list so the detail pane takes the full width. */
export const masterCollapsed = signal<boolean>(initial.masterCollapsed);
/** Transient — Preview tab full-screen mode. Not persisted; resets on reload. */
export const previewFullscreen = signal<boolean>(false);

// Overlay state — only one of {drawer, sheet, palette} should be visible at a time.
export const drawerOpen = signal<DrawerKey | null>(null);
export const sheetOpen = signal<SheetKey | null>(null);
export const paletteOpen = signal(false);

export type DrawerKey = 'settings' | 'files' | 'github' | 'metrics' | 'new-task' | 'memory-edit' | 'trigger-edit';
export type SheetKey = 'task-detail' | 'memory-detail' | 'trigger-detail' | 'new-task' | 'more';

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
