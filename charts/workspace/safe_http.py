"""SSRF-hardened outbound HTTP (stdlib only).

Extracted from server.py's completion-hook delivery path so more than one
caller can reach it. The completion hook posts to exactly one operator-supplied
URL; a Board Processor connector (#588/#589) supplies MANY — the list request,
every pagination `next`, and every step of every action — so the guard has to
be a shared primitive rather than a private detail of one manager.

The three defences, all of which must hold together:

1. **Public-address-only classification.** `public_ip` rejects loopback,
   RFC1918/private, link-local (which covers the cloud metadata service at
   169.254.169.254), multicast, unspecified and reserved. IPv4-mapped IPv6
   (``::ffff:a.b.c.d``) is normalized first so a mapped internal address is
   still caught.

2. **Resolve exactly once, then pin.** `resolve_and_pin` performs a single
   `getaddrinfo` and requires EVERY returned address to be public. The chosen
   IP is then connected to directly while the original hostname is kept for the
   Host header and TLS SNI/certificate validation. Without pinning, a rebinding
   server can answer the check with a public address and the connect with an
   internal one.

3. **Refuse redirects.** A 3xx target is never re-validated, so following one
   hands an attacker-controlled endpoint a free bounce to 127.0.0.1 or an
   in-cluster Service. `NoRedirectHandler` turns any 3xx into an HTTPError.

The opener is also built with an empty `ProxyHandler({})`: an ambient
HTTP(S)_PROXY would route around the pinned IP and reopen the hole.

`allow_internal` is threaded through as a PARAMETER rather than read from an
env var here, so the caller owns the policy (server.py passes its
ALLOW_INTERNAL_HOOKS global, which tests flip).
"""

import http.client
import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request

# Defensive ceiling on any response body we read. A hostile endpoint should not
# be able to stream us unbounded data at delivery/fetch time.
DEFAULT_MAX_BYTES = 1024 * 1024


class SSRFError(Exception):
    """Raised when a target resolves to a non-public address, cannot be
    resolved, or uses an unsupported scheme. Callers surface this as an
    ordinary delivery/fetch error (retried, dead-lettered, or reported) rather
    than silently following it to an internal target."""


def public_ip(addr):
    """Return an ipaddress object for `addr` iff it is a *public* address,
    else None. Normalizes IPv4-mapped IPv6 (``::ffff:a.b.c.d``) to its IPv4
    form before classification so a mapped internal address is still caught.
    The reject set is loopback, RFC1918/private, link-local (covers cloud
    metadata 169.254.169.254), multicast, unspecified and reserved."""
    try:
        ip = ipaddress.ip_address(addr)
    except (ValueError, TypeError):
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_unspecified or ip.is_reserved):
        return None
    return ip


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject *all* redirects.

    Following a 3xx would let an attacker-controlled endpoint that passes the
    public-IP check bounce us to 127.0.0.1 / RFC1918 / 169.254.169.254 / an
    in-cluster service (the redirect target is never re-validated). Returning
    None here makes urllib raise the 3xx as an HTTPError instead of following
    it, so it is surfaced as an error rather than an SSRF."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that connects to a pre-validated, pinned IP while keeping
    the original hostname for the HTTP Host header — so the address we checked
    is the address we actually reach (no second, uncontrolled DNS lookup that
    a rebinding server could answer differently)."""
    def __init__(self, host, pinned_ip, **kw):
        super().__init__(host, **kw)
        self._pinned_ip = pinned_ip

    def connect(self):
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address)
        if self._tunnel_host:
            self._tunnel()


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """As PinnedHTTPConnection, but TLS: connect to the pinned IP yet keep the
    original hostname for SNI and certificate validation."""
    def __init__(self, host, pinned_ip, **kw):
        super().__init__(host, **kw)
        self._pinned_ip = pinned_ip

    def connect(self):
        sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address)
        if self._tunnel_host:
            self.sock = sock
            self._tunnel()
            sock = self.sock
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class PinnedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, pinned_ip):
        super().__init__()
        self._pinned_ip = pinned_ip

    def http_open(self, req):
        return self.do_open(
            lambda host, **kw: PinnedHTTPConnection(host, self._pinned_ip, **kw), req)


class PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, pinned_ip, **kw):
        super().__init__(**kw)
        self._pinned_ip = pinned_ip

    def https_open(self, req):
        return self.do_open(
            lambda host, **kw: PinnedHTTPSConnection(host, self._pinned_ip, **kw), req)


def resolve_and_pin(host, port, *, allow_internal=False):
    """Resolve `host` exactly ONCE and return a single validated IP string to
    connect to. Every returned address (IPv4 and IPv6) must be public, or we
    raise SSRFError — so a rebinding server cannot pass one address at check
    time and serve a different internal one at connect time (there is no
    second lookup). Fails CLOSED on resolution failure.

    With `allow_internal` we still resolve-and-pin (single lookup) but skip the
    public-only classification — the deliberate, documented relaxation for
    single-user / trusted in-cluster deploys."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as e:
        raise SSRFError(f'DNS resolution failed for {host!r}: {e}')
    if not infos:
        raise SSRFError(f'no addresses for {host!r}')
    chosen = None
    for info in infos:
        addr = info[4][0]
        if not allow_internal and public_ip(addr) is None:
            raise SSRFError(f'{host!r} resolves to non-public address {addr}')
        if chosen is None:
            chosen = addr
    return chosen


def is_safe_url(url, *, allow_internal=False):
    """Cheap pre-flight check used at CONFIG time (saving a hook URL, saving a
    connector) so an unusable target is rejected before it is stored. Returns
    a bool and never raises. The authoritative check is still the one inside
    open_pinned at request time — DNS can change between save and use."""
    try:
        parsed = urllib.parse.urlparse(url or '')
    except ValueError:
        return False
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        return False
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    try:
        resolve_and_pin(parsed.hostname, port, allow_internal=allow_internal)
    except SSRFError:
        return False
    return True


def open_pinned(req, *, timeout=10, allow_internal=False):
    """urlopen replacement that pins the connection to a validated IP and
    rejects redirects. Keeps the original hostname for the Host header and TLS
    SNI. Raises SSRFError for an unsafe/unresolvable/unsupported target; other
    errors (HTTPError, URLError, socket timeouts) propagate unchanged so
    caller-side retry/dead-letter logic is unaffected.

    Returns the raw response object — the caller decides whether to read the
    body (see `fetch`) or drain and discard it."""
    parsed = urllib.parse.urlparse(req.full_url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise SSRFError(f'unsupported URL: {req.full_url!r}')
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    pinned = resolve_and_pin(host, port, allow_internal=allow_internal)
    if parsed.scheme == 'https':
        pinned_handler = PinnedHTTPSHandler(pinned)
    else:
        pinned_handler = PinnedHTTPHandler(pinned)
    # Empty ProxyHandler disables any ambient HTTP(S)_PROXY: a proxy would
    # route around our pinned IP and reopen the SSRF hole.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirectHandler, pinned_handler)
    return opener.open(req, timeout=timeout)


def fetch(url, *, method='GET', headers=None, body=None, timeout=30,
          max_bytes=DEFAULT_MAX_BYTES, allow_internal=False):
    """Perform one guarded request and return `(status, headers, body_bytes)`.

    This is the board-side entry point: unlike the completion hook, a board
    fetch NEEDS the response body. The body is capped at `max_bytes` — a
    truncated body will fail JSON parsing downstream, which is the correct
    outcome (a silently truncated page is indistinguishable from a short one).

    A non-2xx status is returned rather than raised, because vendor APIs signal
    rate limits and permission problems through status codes that the caller
    has to inspect (GitHub's secondary limit is a 200 or a 403, not a 429).
    Only transport-level failures and SSRFError propagate.
    """
    req = urllib.request.Request(
        url, data=body, headers=dict(headers or {}), method=method)
    try:
        resp = open_pinned(req, timeout=timeout, allow_internal=allow_internal)
    except urllib.error.HTTPError as e:
        # HTTPError IS a response — read it so callers can see the vendor's
        # error payload (rate-limit hints live there).
        try:
            payload = e.read(max_bytes)
        except Exception:
            payload = b''
        return e.code, dict(getattr(e, 'headers', {}) or {}), payload
    with resp:
        status = getattr(resp, 'status', 200)
        hdrs = dict(getattr(resp, 'headers', {}) or {})
        payload = resp.read(max_bytes)
    return status, hdrs, payload
