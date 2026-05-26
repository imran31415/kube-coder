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
