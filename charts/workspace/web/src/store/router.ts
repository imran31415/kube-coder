import { signal } from '@preact/signals';

/**
 * Tiny history-API router.
 *
 * The SPA can be mounted under several base paths:
 *   /...              (cutover — SPA at root)
 *   /next/...         (legacy direct path during the migration)
 *   /oauth/...        (root through the OAuth2 ingress)
 *   /oauth/next/...   (legacy through the OAuth2 ingress)
 * `normalize()` strips known prefixes so app-level routes are always `/tasks`,
 * `/memory`, etc. The path "/" maps to the default route (/tasks).
 */
const PREFIXES = ['/oauth/next', '/next', '/oauth'];

export function normalize(pathname: string): string {
  let p = pathname || '/';
  for (const pref of PREFIXES) {
    if (p === pref) return '/';
    if (p.startsWith(pref + '/')) return p.slice(pref.length);
  }
  return p;
}

function readCurrent(): string {
  if (typeof window === 'undefined') return '/';
  return normalize(window.location.pathname);
}

export const currentPath = signal<string>(readCurrent());

/** The ingress prefix currently in use (e.g. `/oauth`), or '' at the root. */
function currentPrefix(): string {
  if (typeof window === 'undefined') return '';
  const here = window.location.pathname;
  for (const pref of PREFIXES) {
    if (here === pref || here.startsWith(pref + '/')) return pref;
  }
  return '';
}

/**
 * Build a full URL for an app-level route under the current ingress prefix.
 * Use for the `href` of in-app anchors so right-click/⌘-click "open in new
 * tab" keeps the `/oauth` (or other) prefix the SPA was served from.
 */
export function routeHref(path: string): string {
  return `${currentPrefix()}${path === '/' ? '/' : path}`;
}

export function navigate(path: string, replace = false) {
  if (typeof window === 'undefined') {
    currentPath.value = path;
    return;
  }
  // Keep pushState under the same external URL shape (ingress-aware).
  const url = routeHref(path);
  if (replace) window.history.replaceState({}, '', url);
  else window.history.pushState({}, '', url);
  currentPath.value = path;
}

if (typeof window !== 'undefined') {
  window.addEventListener('popstate', () => {
    currentPath.value = readCurrent();
  });
}

export interface RouteDef {
  path: string;
  title: string;
}

export const ROUTES: RouteDef[] = [
  // Desktop is the default landing route — matchRoute('/') falls through
  // to ROUTES[0], so this order also controls what the user sees on a
  // bare visit to the dashboard.
  { path: '/desktop', title: 'Desktop' },
  { path: '/mission', title: 'Mission Control' },
  { path: '/cto', title: 'AI CTO' },
  { path: '/hypervisor', title: 'Hypervisor' },
  { path: '/walkie', title: 'Walkie-Talkie' },
  { path: '/tasks', title: 'Build' },
  { path: '/memory', title: 'Memory' },
  { path: '/skills', title: 'Skills' },
  { path: '/apps', title: 'Apps' },
  { path: '/triggers', title: 'Triggers' },
  { path: '/files', title: 'Files' },
  { path: '/docs', title: 'Docs' },
  { path: '/settings', title: 'Settings' },
];

export interface NavItem {
  path: string;
  /** Display-label override (e.g. /hypervisor shows as "Chat" per #346);
   *  defaults to the route's ROUTES title. */
  label?: string;
}

export interface NavGroup {
  id: 'mission' | 'workspace' | 'knowledge';
  title: string;
  /** Route the group header itself navigates to (Mission Control → the
   *  /mission board). Groups without a landing page just toggle. */
  landing?: string;
  items: NavItem[];
}

/**
 * Categorized navigation (#267) — purely presentational grouping shared by
 * the Rail, the mobile More sheet, and the command palette. ROUTES and
 * matchRoute() are untouched, so deep links and the default landing route
 * are unaffected by group order. /settings deliberately stays out: it's a
 * standalone item the Rail renders as its own trailing group, not a
 * category member.
 */
export const NAV_GROUPS: NavGroup[] = [
  {
    id: 'mission',
    title: 'Mission Control',
    landing: '/mission',
    items: [
      { path: '/cto', label: 'AI CTO' },
      { path: '/hypervisor', label: 'Chat' },
      { path: '/tasks', label: 'Builds' },
      { path: '/walkie' },
      // Triggers fire builds — agent ops, not workspace plumbing.
      { path: '/triggers' },
    ],
  },
  {
    id: 'workspace',
    title: 'Workspace',
    items: [{ path: '/desktop' }, { path: '/apps' }, { path: '/files' }],
  },
  {
    id: 'knowledge',
    title: 'Knowledge',
    items: [{ path: '/memory' }, { path: '/skills' }, { path: '/docs' }],
  },
];

/**
 * NAV_GROUPS with capability-gated items removed. Currently only `/cto`, hidden
 * when the AI CTO feature is disabled server-side (#467). Pure — callers pass
 * the flags (from serverMode) so this module stays free of store imports.
 */
export function visibleNavGroups(caps: { ctoEnabled?: boolean }): NavGroup[] {
  if (caps.ctoEnabled === false) {
    return NAV_GROUPS.map((g) => ({
      ...g,
      items: g.items.filter((i) => i.path !== '/cto'),
    }));
  }
  return NAV_GROUPS;
}

/** The nav group containing `path` (as landing or item), if any. */
export function navGroupFor(path: string): NavGroup | undefined {
  return NAV_GROUPS.find(
    (g) => g.landing === path || g.items.some((i) => i.path === path),
  );
}

/** Display label for a route in nav surfaces — item override or ROUTES title. */
export function navLabel(path: string): string {
  for (const g of NAV_GROUPS) {
    const item = g.items.find((i) => i.path === path);
    if (item?.label) return item.label;
  }
  return ROUTES.find((r) => r.path === path)?.title ?? path;
}

export function matchRoute(path: string): RouteDef {
  // `/tasks/abc` still matches /tasks (detail handled within the route module).
  if (path === '/' || path === '') return ROUTES[0];
  const top = '/' + path.split('/').filter(Boolean)[0];
  return ROUTES.find((r) => r.path === top) ?? ROUTES[0];
}

/** Everything after the matched top-level segment (no leading slash).
 *  e.g. for `/docs/tasks-concepts` returns `tasks-concepts`. */
export function pathSuffix(path: string): string {
  const parts = (path || '/').split('/').filter(Boolean);
  return parts.slice(1).join('/');
}
