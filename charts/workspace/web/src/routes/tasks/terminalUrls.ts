/**
 * URL extraction shared by the Session tab (TerminalPane) and the Send-message
 * tab (MessageChat). Both surface auto-detected links from the tmux pane so a
 * mobile user — who can't highlight + copy inside the ttyd iframe — can still
 * tap the auth/login URLs assistants like Claude Code routinely print.
 *
 * Server-side `tmux capture-pane -J` already joins logical lines, but we still
 * defensively handle soft-wraps + \r line endings + stray ANSI fragments since
 * pre-`-J` deploys may be in flight and the output.log fallback lacks that
 * protection.
 *
 * Walk-forward parser: anchor at `https?://`, greedily consume URL-safe chars,
 * allow newlines (optionally surrounded by whitespace) to bridge soft-wraps,
 * stop at the first hard delimiter or end of buffer.
 */
const URL_CHAR_RE = /[A-Za-z0-9._~:/?#@!$&'*+,;=%-]/;
const URL_ANCHOR_RE = /https?:\/\//g;
const TRAILING_PUNCT = /[.,;:!?'"`)\]}>]+$/;
const ANSI_RE = /\[[0-9;?]*[a-zA-Z]/g;

export function extractUrls(text: string, max = 5): string[] {
  if (!text) return [];
  // Normalize line endings + strip any ANSI escapes that leaked through.
  const norm = text.replace(/\r\n?/g, '\n').replace(ANSI_RE, '');
  const seen = new Set<string>();
  const out: string[] = [];

  let m: RegExpExecArray | null;
  URL_ANCHOR_RE.lastIndex = 0;
  const found: string[] = [];
  while ((m = URL_ANCHOR_RE.exec(norm)) !== null) {
    let i = m.index + m[0].length;
    let url = m[0];
    while (i < norm.length) {
      const ch = norm[i];
      if (ch === '\n') {
        // Soft-wrap: peek past any whitespace on the next line. If the next
        // non-space char is URL-safe, treat the wrap as join-back.
        let j = i + 1;
        while (j < norm.length && (norm[j] === ' ' || norm[j] === '\t')) j++;
        if (j < norm.length && URL_CHAR_RE.test(norm[j])) {
          i = j;
          continue;
        }
        break;
      }
      // Plain whitespace inside a line is a hard URL terminator.
      if (ch === ' ' || ch === '\t') break;
      if (URL_CHAR_RE.test(ch)) {
        url += ch;
        i++;
        continue;
      }
      break;
    }
    // Strip trailing punctuation that's almost never part of a URL.
    url = url.replace(TRAILING_PUNCT, '');
    if (url.length > m[0].length + 3) found.push(url);
  }

  // Freshest URL first.
  for (let i = found.length - 1; i >= 0 && out.length < max; i--) {
    const u = found[i];
    if (seen.has(u)) continue;
    seen.add(u);
    out.push(u);
  }
  return out;
}

export function shortenUrl(u: string, max = 56): string {
  if (u.length <= max) return u;
  try {
    const url = new URL(u);
    const host = url.host;
    const tail = u.slice(host.length + url.protocol.length + 2);
    const cut = Math.max(8, max - host.length - 5);
    return `${host}/…${tail.slice(-cut)}`;
  } catch {
    return u.slice(0, max - 1) + '…';
  }
}
