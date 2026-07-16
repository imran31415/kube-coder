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
  { path: '/hypervisor', title: 'Hypervisor' },
  { path: '/tasks', title: 'Build' },
  { path: '/memory', title: 'Memory' },
  { path: '/skills', title: 'Skills' },
  { path: '/apps', title: 'Apps' },
  { path: '/triggers', title: 'Triggers' },
  { path: '/files', title: 'Files' },
  { path: '/docs', title: 'Docs' },
  { path: '/settings', title: 'Settings' },
];

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
