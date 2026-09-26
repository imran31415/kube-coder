"""Ordered route tables for server.py's HTTP dispatch (issue #100).

BrowserHandler dispatched every request through hand-maintained if/elif
chains, so a route's priority was wherever its branch happened to sit inside a
500-line method. Overlapping patterns — `/vnc/` the viewer page against
`/vnc/<path>` the noVNC proxy, `/api/webhooks/{id}/test` against the
`/api/webhooks/{id}` receiver — were ordered by an accident of editing, and
the only record of the hazard was a comment asking the next editor not to move
the lines.

A `RouteTable` keeps exactly those semantics (first registration that matches
wins) but turns the order into a short data table that can be read on one
screen and asserted on directly, without issuing a request:

    hit = system.ROUTES.match('GET', '/vnc/core.js', '/vnc/core.js')
    assert hit[0].handler == 'proxy_vnc_request'

Deliberately tiny. There is no middleware stack, no path converters and no
reverse URL building, because registration order is the whole priority model —
which is precisely what the if/elif chains already meant.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Route:
    """One table entry: an HTTP method and path pattern → a handler method."""

    http_method: str
    #: An exact path string, or a compiled regex whose groups become the
    #: handler's positional arguments.
    pattern: object
    #: Name of the BrowserHandler method, resolved with getattr at dispatch
    #: time so overriding or patching it behaves exactly as it did when the
    #: chain called `self.<name>()` directly.
    handler: str
    #: Match the raw request path rather than the normalized one.
    raw_path: bool

    def match(self, path):
        """Capture groups for `path`, or None when the pattern doesn't match.

        An exact (str) pattern captures nothing and yields `()`; a compiled
        regex yields its groups, which the table passes to the handler
        positionally.
        """
        if isinstance(self.pattern, str):
            return () if path == self.pattern else None
        m = self.pattern.match(path)
        return m.groups() if m else None


class RouteTable:
    """An ordered list of `Route`s; the first match wins.

    Registration order IS priority, so a specific pattern has to be added
    before any general one that would also match it. Where that matters the
    registration carries a comment saying so — the point of the table is that
    both routes are then within a few lines of each other, instead of a few
    hundred lines apart in a dispatch method.
    """

    def __init__(self):
        self.routes = []

    def add(self, http_method, pattern, handler, *, raw_path=False):
        """Append a route.

        `pattern` is either an exact path string or a compiled regex — built
        with `re.compile` by the caller so which one it is reads at a glance.
        `handler` is the *name* of a BrowserHandler method.

        `raw_path=True` matches against the request's raw path (query string
        and the SPA's `/oauth` prefix still attached) instead of the
        normalized one. A handful of routes have always been raw-only — the
        kubelet probes and the VNC endpoints — and stay that way.
        """
        if isinstance(pattern, str) and not pattern.startswith('/'):
            raise ValueError(f'route pattern must be a path: {pattern!r}')
        self.routes.append(Route(http_method, pattern, handler, raw_path))

    def match(self, http_method, path, raw_path):
        """First `(route, args)` whose method and pattern match, else None."""
        for route in self.routes:
            if route.http_method != http_method:
                continue
            args = route.match(raw_path if route.raw_path else path)
            if args is not None:
                return route, args
        return None

    def dispatch(self, request, http_method, path, raw_path):
        """Invoke the first matching route on `request`. True if handled."""
        hit = self.match(http_method, path, raw_path)
        if hit is None:
            return False
        route, args = hit
        getattr(request, route.handler)(*args)
        return True
