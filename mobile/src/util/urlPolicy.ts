/**
 * Transport / URL security policy for credentialed connections.
 *
 * The app sends a Bearer token on every workspace request and the controller
 * ADMIN token on every controller request. Sending either over cleartext HTTP
 * lets a network-positioned attacker capture or tamper with the credential
 * (Finding 6 of the July 2026 source review). These pure helpers decide whether
 * a user-supplied host may receive a credentialed request, and never depend on
 * React Native APIs so they are trivially unit-testable.
 *
 * Policy, in one sentence per connection kind:
 *   - controller: HTTPS is REQUIRED, always. The admin token must never travel
 *     over plain HTTP, on any host, loopback or not.
 *   - workspace:  HTTPS is required for every non-loopback host. Plain HTTP is
 *     permitted ONLY for http://localhost, http://127.0.0.1 (127.0.0.0/8) and
 *     http://[::1], and ONLY when the build allows cleartext (dev/preview).
 *
 * The matching build-time transport config lives in app.config.ts: production
 * builds ship with iOS `NSAllowsArbitraryLoads: false` and Android
 * `usesCleartextTraffic: false`, so even if this runtime check were bypassed the
 * OS would refuse the cleartext socket.
 */

export type HostKind = 'workspace' | 'controller';

export interface HostPolicyResult {
  /** True when the host may receive a credentialed request. */
  ok: boolean;
  /** Normalized base URL (trailing slashes stripped) — present only when ok. */
  url?: string;
  /** User-facing reason the host was rejected — present only when !ok. */
  reason?: string;
}

/**
 * Whether this build permits cleartext HTTP to loopback at all. Driven by the
 * public env var EXPO_PUBLIC_ALLOW_CLEARTEXT, which eas.json sets for the
 * `development` and `preview` profiles and leaves unset for `production`. Kept
 * in sync with the ATS / usesCleartextTraffic gate in app.config.ts.
 */
export function isCleartextAllowed(): boolean {
  const v = (process.env.EXPO_PUBLIC_ALLOW_CLEARTEXT ?? '').trim().toLowerCase();
  return v === '1' || v === 'true' || v === 'yes';
}

interface ParsedUrl {
  scheme: string;
  hostname: string;
  port: string;
}

/**
 * Minimal, dependency-free URL parser — RN's global URL is incomplete and we
 * only need scheme/host/port. Returns null when the string isn't a usable
 * absolute URL.
 */
function parseUrl(raw: string): ParsedUrl | null {
  const s = (raw ?? '').trim();
  const m = /^([a-zA-Z][a-zA-Z0-9+.-]*):\/\/([^/?#]*)([/?#]|$)/.exec(s);
  if (!m) return null;
  const scheme = m[1].toLowerCase();
  let authority = m[2];
  if (!authority) return null;
  // Drop any userinfo (user:pass@host).
  const at = authority.lastIndexOf('@');
  if (at >= 0) authority = authority.slice(at + 1);
  let hostname: string;
  let port = '';
  if (authority.startsWith('[')) {
    // IPv6 literal, e.g. [::1]:6080
    const end = authority.indexOf(']');
    if (end < 0) return null;
    hostname = authority.slice(1, end);
    const rest = authority.slice(end + 1);
    if (rest.startsWith(':')) port = rest.slice(1);
  } else {
    const colon = authority.indexOf(':');
    if (colon >= 0) {
      hostname = authority.slice(0, colon);
      port = authority.slice(colon + 1);
    } else {
      hostname = authority;
    }
  }
  if (!hostname) return null;
  return { scheme, hostname: hostname.toLowerCase(), port };
}

/** Strip trailing slashes so a base URL concatenates cleanly with a path. */
export function normalizeBaseUrl(raw: string): string {
  return (raw ?? '').trim().replace(/\/+$/, '');
}

/**
 * Loopback hostnames that are safe over plain HTTP because traffic never leaves
 * the device: localhost, the IPv4 loopback block 127.0.0.0/8, and the IPv6
 * loopback ::1. A LAN/private address (192.168.x, 10.x, a `.local` name, a Wi-Fi
 * host) is NOT loopback — it can be intercepted — and is intentionally excluded.
 */
export function isLoopbackHostname(hostname: string): boolean {
  const h = (hostname ?? '').toLowerCase().replace(/^\[/, '').replace(/\]$/, '');
  if (h === 'localhost') return true;
  if (h === '::1' || h === '0:0:0:0:0:0:0:1') return true;
  if (/^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(h)) return true;
  return false;
}

const CONTROLLER_HTTP_REASON =
  'The controller admin token must never be sent over plain HTTP — it authorizes ' +
  'administrative workspace operations. Use an https:// controller URL.';

/**
 * Decide whether a user-supplied host may receive a credentialed request.
 *
 * @param raw   the raw host string the user typed
 * @param kind  'workspace' (Bearer token) or 'controller' (admin token)
 * @param opts.allowCleartext  override the build gate (defaults to
 *              isCleartextAllowed()); pass explicitly in tests.
 */
export function validateHost(
  raw: string,
  kind: HostKind,
  opts: { allowCleartext?: boolean } = {},
): HostPolicyResult {
  const allowCleartext = opts.allowCleartext ?? isCleartextAllowed();
  const trimmed = (raw ?? '').trim();
  if (!trimmed) return { ok: false, reason: 'Enter a host URL.' };

  const parsed = parseUrl(trimmed);
  if (!parsed) {
    return {
      ok: false,
      reason: 'Enter a full URL starting with https:// (for example https://you.kube-coder.example.com).',
    };
  }

  const { scheme, hostname } = parsed;
  if (scheme !== 'http' && scheme !== 'https') {
    return { ok: false, reason: `Unsupported URL scheme "${scheme}://". Use https://.` };
  }

  // HTTPS is always acceptable for both kinds.
  if (scheme === 'https') {
    return { ok: true, url: normalizeBaseUrl(trimmed) };
  }

  // From here scheme === 'http' (cleartext).
  if (kind === 'controller') {
    return { ok: false, reason: CONTROLLER_HTTP_REASON };
  }

  // Workspace over http: only loopback, and only when the build permits it.
  if (!isLoopbackHostname(hostname)) {
    return {
      ok: false,
      reason:
        `Plain HTTP is only allowed for localhost. Use https:// for "${hostname}" so your ` +
        `API token isn't transmitted in the clear where it could be captured.`,
    };
  }
  if (!allowCleartext) {
    return {
      ok: false,
      reason:
        'This build blocks cleartext HTTP. Use an https:// host, or install a development ' +
        'build to reach a local http://localhost workspace.',
    };
  }
  return { ok: true, url: normalizeBaseUrl(trimmed) };
}

/**
 * True when moving from `fromUrl` to `toUrl` is a security downgrade
 * (HTTPS → HTTP). Unparseable URLs are treated as a downgrade (fail closed).
 * Used to reject a redirect that would move a credentialed request onto
 * cleartext.
 */
export function isDowngrade(fromUrl: string, toUrl: string): boolean {
  const a = parseUrl(fromUrl);
  const b = parseUrl(toUrl);
  if (!a || !b) return true;
  return a.scheme === 'https' && b.scheme === 'http';
}

function effectivePort(p: ParsedUrl): string {
  if (p.port) return p.port;
  return p.scheme === 'https' ? '443' : p.scheme === 'http' ? '80' : '';
}

/** True when two URLs share scheme + hostname + effective port (same origin). */
export function sameOrigin(a: string, b: string): boolean {
  const pa = parseUrl(a);
  const pb = parseUrl(b);
  if (!pa || !pb) return false;
  return pa.scheme === pb.scheme && pa.hostname === pb.hostname && effectivePort(pa) === effectivePort(pb);
}

/**
 * Whether the Authorization header may be carried onto a redirect target.
 * Returns true ONLY for a same-origin, non-downgrading hop; a cross-origin hop
 * or an HTTPS→HTTP downgrade returns false so the credential is never leaked to
 * a different (or cleartext) origin.
 *
 * NOTE: React Native's `fetch` auto-follows redirects and does not expose the
 * intermediate Location, so the API client cannot call this per-hop at runtime.
 * The client instead takes the stricter route of rejecting ANY redirect on a
 * credentialed request (see api/client.ts). This function encodes the header
 * policy explicitly and is the unit-tested decision point.
 */
export function shouldForwardAuthOnRedirect(originalUrl: string, redirectUrl: string): boolean {
  if (isDowngrade(originalUrl, redirectUrl)) return false;
  return sameOrigin(originalUrl, redirectUrl);
}
