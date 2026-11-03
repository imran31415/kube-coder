/**
 * Which workspace page a phone-style embed URL opens (#833).
 *
 * The phone loads apps and the terminal through the session bootstrap
 * `/api/claude/apps/session?next=<path>`; the desktop app hands `<path>` to
 * its core instead (DesktopEmbed). Pure, for the node-side vitest.
 */

/** The `next` path of an `/api/claude/apps/session?next=…` bootstrap URL. */
export function embedNext(uri: string): string | null {
  const m = /\/api\/claude\/apps\/session\?(?:.*&)?next=([^&#]+)/.exec(uri);
  if (!m) return null;
  try {
    return decodeURIComponent(m[1]);
  } catch {
    return null;
  }
}
