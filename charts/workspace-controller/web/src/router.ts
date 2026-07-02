import { signal } from '@preact/signals';

// Hash routing keeps deep links (#/w/<user>) entirely client-side — the
// controller/oauth2-proxy always just serve index.html, so refresh + bookmarks
// work with no server route handling.
function current(): string {
  return location.hash.slice(1) || '/';
}

export const route = signal<string>(current());

window.addEventListener('hashchange', () => {
  route.value = current();
});

export function navigate(path: string): void {
  location.hash = path;
}

/** If the route is /w/<user>, return <user>; otherwise null. */
export function detailUser(path: string): string | null {
  const m = /^\/w\/([a-z0-9-]{1,41})$/.exec(path);
  return m ? m[1] : null;
}

/** The full cluster-resources drill-down (capacity history + per-node + insights). */
export function isCapacityRoute(path: string): boolean {
  return path === '/capacity';
}

/** True for the provision form/status routes (/provision, /provision/<slug>). */
export function isProvisionRoute(path: string): boolean {
  return path === '/provision' || path.startsWith('/provision/') || path.startsWith('/provision?');
}

/** The slug being watched on /provision/<slug>, or null on the bare form. */
export function provisionSlug(path: string): string | null {
  const m = /^\/provision\/([a-z0-9-]{1,41})/.exec(path);
  return m ? m[1] : null;
}

/** The ?error=… message the manifest callback redirects back with, or null. */
export function provisionError(path: string): string | null {
  const q = path.indexOf('?');
  if (q < 0) return null;
  return new URLSearchParams(path.slice(q + 1)).get('error');
}
