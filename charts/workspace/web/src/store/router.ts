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

export function navigate(path: string, replace = false) {
  if (typeof window === 'undefined') {
    currentPath.value = path;
    return;
  }
  // Find which prefix is currently in use so pushState keeps the user under
  // the same external URL shape (ingress-aware).
  const here = window.location.pathname;
  let prefix = '';
  for (const pref of PREFIXES) {
    if (here === pref || here.startsWith(pref + '/')) {
      prefix = pref;
      break;
    }
  }
  const url = `${prefix}${path === '/' ? '/' : path}`;
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
  { path: '/tasks', title: 'Tasks' },
  { path: '/memory', title: 'Memory' },
  { path: '/triggers', title: 'Triggers' },
  { path: '/files', title: 'Files' },
  { path: '/settings', title: 'Settings' },
];

export function matchRoute(path: string): RouteDef {
  // `/tasks/abc` still matches /tasks (detail handled within the route module).
  if (path === '/' || path === '') return ROUTES[0];
  const top = '/' + path.split('/').filter(Boolean)[0];
  return ROUTES.find((r) => r.path === top) ?? ROUTES[0];
}
