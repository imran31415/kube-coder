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
 * `/memory`, etc. The path "/" maps to the default route (HOME_PATH, Chat).
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

/**
 * The landing route: what a bare visit to the dashboard (`/`), an unknown
 * path, and the brand mark all open. Chat is the front door — it is where
 * work starts, and Mission Control is one click away inside Workspace for
 * the "what needs me?" sweep. Every other route still deep-links as before.
 */
export const HOME_PATH = '/hypervisor';

export const ROUTES: RouteDef[] = [
  // "Chat" is the one user-facing name for this surface (#346); "Hypervisor"
  // survives only as the internal term (route path, stores, server modules).
  // Listed first because it is HOME_PATH, so the command palette's jump list
  // starts where the rail does.
  { path: '/hypervisor', title: 'Chat' },
  { path: '/mission', title: 'Mission Control' },
  { path: '/desktop', title: 'Desktop' },
  // Kept resolvable, not navigable (#683): /cto is a permanent redirect into
  // Chat with CTO mode pre-selected, so old bookmarks and doc links still land
  // somewhere sensible. It is deliberately absent from NAV_GROUPS below.
  { path: '/cto', title: 'AI CTO' },
  { path: '/board', title: 'Board' },
  { path: '/feed', title: 'Feed' },
  { path: '/walkie', title: 'Walkie-Talkie' },
  { path: '/tasks', title: 'Build' },
  { path: '/memory', title: 'Memory' },
  { path: '/skills', title: 'Skills' },
  { path: '/apps', title: 'Apps' },
  { path: '/security', title: 'Security' },
  { path: '/triggers', title: 'Triggers' },
  { path: '/files', title: 'Files' },
  { path: '/docs', title: 'Docs' },
  { path: '/settings', title: 'Settings' },
];

export interface NavItem {
  path: string;
  /** Display-label override (e.g. /tasks shows as "Builds"); defaults to the
   *  route's ROUTES title. */
  label?: string;
}

export interface NavGroup {
  id: 'mission' | 'machine' | 'knowledge';
  title: string;
  /** Route the section's "Overview" entry opens (Mission Control → the
   *  /mission board). Sections without a landing page are just a heading. */
  landing?: string;
  items: NavItem[];
}

/**
 * The two top-level nav entries, in order: Chat, then the Workspace
 * disclosure. Everything that is not Chat lives inside Workspace —
 * NAV_GROUPS below are its sub-headings, and Settings closes it.
 */
export const NAV_TOP = [
  { id: 'chat', title: 'Chat', path: HOME_PATH },
  { id: 'workspace', title: 'Workspace' },
] as const;

/** The id of the one collapsible top-level group (the Rail's disclosure). */
export const WORKSPACE_GROUP_ID = 'workspace';

/** Routes listed inside Workspace after its sections, below a divider. */
export const WORKSPACE_TAIL: readonly string[] = ['/settings'];

/**
 * The sections inside the Workspace disclosure (#267, regrouped when the
 * sidebar collapsed to Chat + Workspace) — purely presentational grouping
 * shared by the Rail, the narrow-screen Workspace sheet and the command
 * palette. ROUTES and matchRoute() are untouched, so deep links are
 * unaffected by group order.
 *
 * The former "Workspace" section is now "Machine": Workspace is the
 * top-level container, and a sub-heading with the same name as the thing
 * that contains it reads as a mistake. What the section holds — Desktop,
 * Apps, Files — is the pod itself, so it is named for that. (The mobile
 * drawer's copy adds Metrics, which is machine health too.)
 */
export const NAV_GROUPS: NavGroup[] = [
  {
    id: 'mission',
    title: 'Mission Control',
    landing: '/mission',
    items: [
      // The Board Processor works someone ELSE's backlog, so it sits with the
      // other "what needs me?" surfaces rather than under Machine.
      { path: '/board', label: 'Board' },
      { path: '/feed', label: 'Feed' },
      { path: '/tasks', label: 'Builds' },
      // Security scans (#726) answer "what needs me?" about the app the
      // user is building, which is the question this group is for.
      { path: '/security' },
      { path: '/walkie' },
      // Triggers fire builds — agent ops, not workspace plumbing.
      { path: '/triggers' },
    ],
  },
  {
    id: 'machine',
    title: 'Machine',
    items: [{ path: '/desktop' }, { path: '/apps' }, { path: '/files' }],
  },
  {
    id: 'knowledge',
    title: 'Knowledge',
    items: [{ path: '/memory' }, { path: '/skills' }, { path: '/docs' }],
  },
];

/**
 * NAV_GROUPS with capability-gated items removed. Pure — callers pass the flags
 * (from serverMode) so this module stays free of store imports.
 *
 * `/feed` hides when the AI CTO feature is off (#467/#470 — the Feed rides it);
 * `/board` hides when the Board Processor is off (#588/#589). They are
 * INDEPENDENT gates: a workspace can work an external board without running an
 * AI CTO, and vice versa. `/cto` is no longer listed at all (#683) — with the
 * page folded into Chat, `ctoEnabled` gates the Mode picker, not a destination.
 */
export function visibleNavGroups(caps: {
  ctoEnabled?: boolean;
  boardEnabled?: boolean;
  scansEnabled?: boolean;
}): NavGroup[] {
  const hidden = new Set<string>();
  if (caps.ctoEnabled === false) hidden.add('/feed');
  if (caps.boardEnabled === false) hidden.add('/board');
  // Scanning needs a container runtime the workspace may not have, so unlike
  // the gates above this one is off unless the deployment turned it on.
  if (caps.scansEnabled !== true) hidden.add('/security');
  if (hidden.size === 0) return NAV_GROUPS;
  return NAV_GROUPS.map((g) => ({
    ...g,
    items: g.items.filter((i) => !hidden.has(i.path)),
  }));
}

/** The Workspace section containing `path` (as landing or item), if any. */
export function navGroupFor(path: string): NavGroup | undefined {
  return NAV_GROUPS.find(
    (g) => g.landing === path || g.items.some((i) => i.path === path),
  );
}

/** True when `path` is reached through the Workspace disclosure — i.e. it is
 *  anything the nav lists other than Chat. */
export function inWorkspace(path: string): boolean {
  return !!navGroupFor(path) || WORKSPACE_TAIL.includes(path);
}

/** Display label for a route in nav surfaces — item override or ROUTES title. */
export function navLabel(path: string): string {
  for (const g of NAV_GROUPS) {
    const item = g.items.find((i) => i.path === path);
    if (item?.label) return item.label;
  }
  return ROUTES.find((r) => r.path === path)?.title ?? path;
}

/** The home route's definition (HOME_PATH is always in ROUTES). */
function homeRoute(): RouteDef {
  return ROUTES.find((r) => r.path === HOME_PATH)!;
}

export function matchRoute(path: string): RouteDef {
  // `/tasks/abc` still matches /tasks (detail handled within the route module).
  if (path === '/' || path === '') return homeRoute();
  const top = '/' + path.split('/').filter(Boolean)[0];
  return ROUTES.find((r) => r.path === top) ?? homeRoute();
}

/** Everything after the matched top-level segment (no leading slash).
 *  e.g. for `/docs/tasks-concepts` returns `tasks-concepts`. */
export function pathSuffix(path: string): string {
  const parts = (path || '/').split('/').filter(Boolean);
  return parts.slice(1).join('/');
}
