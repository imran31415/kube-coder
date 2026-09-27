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

import urllib.parse
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
    #: Match the request's RAW path (query string and the SPA's `/oauth`
    #: prefix still attached) instead of the normalized one. A handful of
    #: routes have always been raw-only — the kubelet probes, /metrics and
    #: the VNC endpoints — and stay that way.
    raw_path: bool
    #: Pass the request's parsed query string to the handler as its first
    #: argument, ahead of any capture groups. The chains computed that dict
    #: inline before calling the few handlers that read query parameters.
    query: bool
    #: Drop the query string before matching. do_GET strips it up front, but
    #: every other verb routes on `_strip_route_prefix(self.path)`, which
    #: keeps it — so a route whose parameters ride the query
    #: (`DELETE /api/files?path=…`) needs this to match at all. The chain
    #: spelled it `path.split('?', 1)[0] == '/api/files'` per branch.
    strip_query: bool
    #: Attribute names to stash the leading capture groups on, one per name.
    #: A sizeable family of handlers reads its parameters off the request
    #: (`self._webhook_id`) rather than taking them as arguments, and the
    #: chain assigned them at the dispatch site; this is that assignment, as
    #: data. Groups past the named ones are still passed positionally.
    #: Giving those handlers real parameters is a later cleanup — doing it
    #: now would mean editing bodies this series moves verbatim.
    sets: tuple

    def match(self, path):
        """Capture groups for `path`, or None when the pattern doesn't match.

        An exact (str) pattern captures nothing and yields `()`; a compiled
        regex yields its groups, which the table passes to the handler
        positionally.
        """
        if self.strip_query:
            path = path.split('?', 1)[0]
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

    def add(self, http_method, pattern, handler, *, raw_path=False,
            query=False, strip_query=False, sets=()):
        """Append a route. Each keyword is one column — see `Route`.

        `pattern` is either an exact path string or a compiled regex — built
        with `re.compile` by the caller so which one it is reads at a glance.
        `handler` is the *name* of a BrowserHandler method. `sets` accepts a
        single attribute name as well as a tuple of them.
        """
        if isinstance(pattern, str) and not pattern.startswith('/'):
            raise ValueError(f'route pattern must be a path: {pattern!r}')
        if isinstance(sets, str):
            sets = (sets,)
        self.routes.append(Route(http_method, pattern, handler, raw_path,
                                 query, strip_query, tuple(sets)))

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
        for name, value in zip(route.sets, args):
            setattr(request, name, value)
        args = args[len(route.sets):]
        if route.query:
            args = (parse_query(raw_path),) + args
        getattr(request, route.handler)(*args)
        return True


def parse_query(raw_path):
    """`parse_qs` over a raw request path's query string.

    Exactly what do_GET computed inline as `memory_query` before calling a
    handler that reads query parameters — the raw path is used because the
    normalized one has already had the query string stripped off.
    """
    query_string = raw_path.split('?', 1)[1] if '?' in raw_path else ''
    return urllib.parse.parse_qs(query_string)
