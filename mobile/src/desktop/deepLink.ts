/**
 * `kubecoder://` deep links (#833) → the push target format.
 *
 * A link from WhatsApp, a browser or a second launch of the desktop app names
 * the same things a push notification does, so it is turned into the same
 * `data` a push carries (`{ ref }`) and routed by the same code
 * (`handleNotificationTap` in src/push/notifications.ts). Two spellings:
 *
 *   kubecoder://board/<boardId>/<itemId>   kubecoder://board:<boardId>:<itemId>
 *   kubecoder://task/<id>                  kubecoder://task:<id>
 *   kubecoder://thread/<id>                kubecoder://thread:<id>
 *   kubecoder://scan/<id>                  kubecoder://scan:<id>
 *   kubecoder://feed                       (the Feed)
 *
 * The ref spelling is exactly a push `data.ref`, item ids with colons
 * included. Anything else yields an empty ref, which routes to the Feed, so a
 * link is never a dead end — and never an `href`: a link cannot make the app
 * open some other URL.
 *
 * Pure, for the node-side vitest.
 */
import type { PushData } from '../util/push';

const KINDS = new Set(['board', 'task', 'thread', 'scan', 'memory']);
const MAX_LEN = 2048;

function decode(seg: string): string | null {
  try {
    return decodeURIComponent(seg);
  } catch {
    return null;
  }
}

/** The push ref a `kubecoder://` URL points at, or '' for "open the Feed". */
export function refFromDeepLink(raw: string): string {
  const s = (raw ?? '').trim();
  if (!s || s.length > MAX_LEN) return '';
  const m = /^kubecoder:(?:\/\/)?(.*)$/i.exec(s);
  if (!m) return '';
  // Drop a query or fragment; ids never need one.
  const rest = m[1].split(/[?#]/)[0].replace(/\/+$/, '');
  if (!rest) return '';

  const colon = rest.indexOf(':');
  const slash = rest.indexOf('/');
  if (colon > 0 && (slash < 0 || colon < slash)) {
    // The ref spelling: everything after the scheme IS the ref.
    const ref = decode(rest);
    if (!ref) return '';
    const kind = ref.slice(0, ref.indexOf(':')).toLowerCase();
    return KINDS.has(kind) ? `${kind}${ref.slice(ref.indexOf(':'))}` : '';
  }

  const parts = rest.split('/').map(decode);
  if (parts.some((p) => p === null || p === '')) return '';
  const [kind, ...ids] = (parts as string[]).map((p, i) => (i === 0 ? p.toLowerCase() : p));
  if (kind === 'board' && ids.length === 2) return `board:${ids[0]}:${ids[1]}`;
  if ((kind === 'task' || kind === 'thread' || kind === 'scan') && ids.length === 1) {
    return `${kind}:${ids[0]}`;
  }
  return '';
}

/** A queued desktop target (a deep link, or a notification click carrying
 *  the alert's ref and Feed row) as the push `data` the tap router takes. */
export function pushDataFromTarget(t: { url?: string; ref?: string; feedId?: string }): PushData {
  if (t.url) return { ref: refFromDeepLink(t.url) };
  return { ref: t.ref ?? '', feedId: t.feedId ?? '' };
}
