"""Shared harness for the route-table suites (#100).

Three things every per-domain suite wants: a real server to make requests
against, a one-line way to ask a table what it would do with a path, and the
handful of checks that are the same question asked of a different table.

`EndpointTestCase` is the socket. A dozen suites under `tests/` hand-rolled
the same three things — pick a port, start a ThreadingHTTPServer on
BrowserHandler in a daemon thread, and issue requests that yield a status code
even when it is an error one (`urllib` raises on 4xx/5xx, so every suite
caught HTTPError itself).

`RouteTableTestCase` and `RouteEndpointTestCase` are the checks. By the
seventh domain each suite was retyping the same six tests over its own table:
does every route name a method that exists, do near-miss paths stay
unmatched, does every route survive the SPA's `/oauth` prefix, is the verb
part of the match, and which routes carry the `query=` / `strip_query=`
columns. Only the data differed, so the data is what a suite declares now.

Two checks here were in some suites and not others, and are now everywhere:
the capture-group/signature agreement (a group that is neither `sets=` nor a
declared parameter is a TypeError at request time) and the exact-set form of
the column assertions (declaring nothing asserts that no route carries the
column, rather than checking nothing).

Deliberately only that. A domain's own hazards — `/vnc/` against the noVNC
proxy, `/test` against the webhook receiver, the reserved board ids — are the
part worth writing by hand, and stay in the suite. So does any fixture: a temp
DOCS_DIR, a patched manager, the auth bypass. These base classes know about
the socket and the table, nothing else.
"""

import http.server
import inspect
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402


class EndpointTestCase(unittest.TestCase):
    """One server per test class, on a port the kernel picks.

    AUTH_MODE is pinned to oauth2 by default — the mode where server.py is
    its own enforcer, so an auth-gated route answers 401 instead of doing
    real work. A 401 is also what separates "the route matched" from "it fell
    through to the 404", which is most of what a routing test wants to know.
    """

    #: AUTH_MODE for the lifetime of the class; restored on teardown.
    auth_mode = 'oauth2'

    @classmethod
    def setUpClass(cls):
        cls._auth_mode_save = server.AUTH_MODE
        server.AUTH_MODE = cls.auth_mode
        # Port 0 lets the kernel pick a free one, with no window between
        # probing for it and binding it.
        cls.httpd = http.server.ThreadingHTTPServer(
            ('127.0.0.1', 0), server.BrowserHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server.AUTH_MODE = cls._auth_mode_save

    def request(self, path, method='GET', body=None):
        """Return `(status, body)` whether or not the response was an error."""
        req = urllib.request.Request(
            f'http://127.0.0.1:{self.port}{path}', data=body, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def get(self, path):
        return self.request(path)

    def post(self, path, body=b''):
        return self.request(path, method='POST', body=body)


def handler_for(table, http_method, path, raw=None):
    """The handler name `table` would dispatch `path` to, or None.

    The question every route-order test asks. `raw` defaults to `path`, which
    is the right answer for a normalized-path route; pass it explicitly to
    check a raw-path route or a request that arrived under `/oauth`.
    """
    hit = table.match(http_method, path, path if raw is None else raw)
    return hit[0].handler if hit else None


def dispatch_to_mock(table, http_method, path, raw=None):
    """Dispatch `path` at a stand-in request and return it.

    The way to check a `sets=` column: the returned Mock carries whatever the
    table assigned (`_webhook_id`, `_claude_task_id`, …) and records the call
    the handler received. Asserts that something matched, because a test
    inspecting the result has already established that it does.
    """
    request = mock.Mock(spec=server.BrowserHandler)
    matched = table.dispatch(
        request, http_method, path, path if raw is None else raw)
    assert matched, f'{http_method} {path} matched no route'
    return request


def args_for(table, http_method, path, raw=None):
    """The positional arguments the table would hand the handler.

    `()` for an exact route, the capture groups for a regex one — and the
    parsed query string ahead of them on a `query=True` route. Raises if
    nothing matched, because a test asserting on arguments has already
    established that it does.
    """
    return table.match(http_method, path, path if raw is None else raw)[1]


class DomainRouteTests:
    """The checks that are the same question asked of a different table.

    A **mixin**, not a TestCase: `class FooRouteTests(DomainRouteTests,
    unittest.TestCase)`. Mixed in rather than subclassed so that importing it
    into a suite does not also collect it as a test class of its own, with no
    table to run against.

    A suite points `table` at its ROUTES and fills in the samples below.
    Everything a domain actually has to think about — which route must precede
    which, what a capture group means, why an id charset is what it is — still
    gets written by hand in the suite.
    """

    #: The domain's RouteTable.
    table = None
    #: `(http_method, path)` pairs that must NOT resolve in this table:
    #: near-misses, trailing slashes, a segment too many, a neighbouring
    #: prefix. The suite's answer to "where does this domain stop".
    foreign_paths = ()
    #: `(http_method, path)` pairs that must still resolve when the request
    #: arrives under the SPA's `/oauth` prefix. One per shape is enough.
    oauth_samples = ()
    #: `(http_method, path)` pairs where the path is real but the verb is not.
    wrong_verb_samples = ()
    #: Handler names whose route carries `query=True`. Exact: anything not
    #: named here must NOT carry it, so an empty set is a real assertion.
    query_handlers = frozenset()
    #: Handler names whose route carries `strip_query=True`. Exact, as above.
    strip_query_handlers = frozenset()
    #: `(http_method, path)` pairs owned by routes this table was hoisted
    #: ABOVE. A domain whose branches were interleaved with another's rather
    #: than contiguous cannot be collapsed into one dispatch point without
    #: moving its later routes over everything in between; listing what they
    #: jumped means a route added here later that WOULD shadow one of them
    #: fails in CI rather than in production. Empty for a domain that
    #: occupied an unbroken run of elif branches and hoisted nothing.
    hoisted_over_paths = ()
    #: The literal path prefixes this domain owns. Declared alongside
    #: `hoisted_over_paths` to show the hoist is safe structurally, not just
    #: for the paths that happen to be sampled.
    owned_prefixes = ()

    def resolve(self, http_method, path, raw=None):
        """The handler name this table would dispatch to, or None."""
        return handler_for(self.table, http_method, path, raw)

    def test_the_suite_declares_its_samples(self):
        # The three sample lists are the only checks here that cannot fail
        # on an empty declaration, so mixing this in without filling them in
        # would pass vacuously. (The two column sets are exact-set
        # comparisons, which assert something either way.)
        for name in ('foreign_paths', 'oauth_samples', 'wrong_verb_samples'):
            self.assertTrue(getattr(self, name),
                            f'{type(self).__name__} declares no {name}')

    def test_every_route_names_a_real_handler_method(self):
        for route in self.table.routes:
            self.assertTrue(hasattr(server.BrowserHandler, route.handler),
                            f'{route} names a method BrowserHandler lacks')

    def test_paths_outside_the_domain_do_not_match(self):
        for http_method, path in self.foreign_paths:
            self.assertIsNone(self.resolve(http_method, path),
                              f'{http_method} {path}')

    def test_every_route_matches_the_normalized_path(self):
        # The SPA prefixes its calls with /oauth in oauth2 mode, and do_GET
        # matches these against the stripped path.
        for http_method, path in self.oauth_samples:
            self.assertIsNotNone(
                self.resolve(http_method, path, raw='/oauth' + path),
                f'{http_method} {path}')

    def test_the_verb_is_part_of_the_match(self):
        for http_method, path in self.wrong_verb_samples:
            self.assertIsNone(self.resolve(http_method, path),
                              f'{http_method} {path}')

    def test_no_route_matches_a_path_it_was_hoisted_above(self):
        for http_method, path in self.hoisted_over_paths:
            self.assertIsNone(
                self.resolve(http_method, path),
                f'{http_method} {path} is now shadowed by this table')

    def test_the_hoisted_over_paths_are_outside_this_domains_prefixes(self):
        # The structural reason the check above passes, rather than a
        # property of the sample: disjoint literal prefixes.
        if not self.hoisted_over_paths:
            return
        self.assertTrue(self.owned_prefixes,
                        f'{type(self).__name__} hoisted over paths but '
                        f'declares no owned_prefixes')
        for _http_method, path in self.hoisted_over_paths:
            self.assertFalse(
                any(path.startswith(p) for p in self.owned_prefixes),
                f'{path} shares a prefix with this domain')

    def test_only_the_declared_handlers_read_the_query_string(self):
        self.assertEqual({r.handler for r in self.table.routes if r.query},
                         set(self.query_handlers))

    def test_only_the_declared_handlers_strip_the_query_string(self):
        self.assertEqual(
            {r.handler for r in self.table.routes if r.strip_query},
            set(self.strip_query_handlers))

    def test_every_capture_group_is_either_set_or_declared_by_the_handler(self):
        # A group that is neither named in `sets=` nor a parameter of the
        # handler would be passed to a method that does not take it — a
        # TypeError at request time rather than a routing miss. `query=True`
        # prepends one more argument, so it counts too.
        for route in self.table.routes:
            groups = 0 if isinstance(route.pattern, str) else route.pattern.groups
            passed = groups - len(route.sets) + (1 if route.query else 0)
            declared = [
                name for name, p in inspect.signature(
                    getattr(server.BrowserHandler, route.handler)
                ).parameters.items()
                if name != 'self' and p.default is inspect.Parameter.empty
            ]
            self.assertEqual(passed, len(declared),
                             f'{route.handler}: {passed} argument(s) from the '
                             f'route vs {len(declared)} parameter(s)')


class DomainEndpointTests:
    """The two end-to-end checks every domain suite was also retyping.

    A mixin over `EndpointTestCase`, for the reason above: `class
    FooEndpointTests(DomainEndpointTests, EndpointTestCase)`.

    Auth gating stays hand-written per suite: which routes answer 401, and
    which authenticate on their own terms instead, is the domain's own fact.
    """

    #: `(http_method, path)` pairs that must reach their handler — and so
    #: answer 401 rather than 404 — when prefixed with `/oauth`.
    oauth_reachable = ()
    #: `(http_method, path)` pairs that must fall through to a 404. The
    #: end-to-end form of `foreign_paths`: it also proves the fall-through
    #: still 404s rather than being answered with the SPA shell.
    unmatched_shapes = ()

    def test_reachable_under_the_oauth_prefix(self):
        self.assertTrue(self.oauth_reachable,
                        f'{type(self).__name__} declares no oauth_reachable')
        for http_method, path in self.oauth_reachable:
            self.assertEqual(
                self.request('/oauth' + path, method=http_method)[0], 401,
                f'{http_method} {path}')

    def test_unmatched_shapes_stay_404(self):
        self.assertTrue(self.unmatched_shapes,
                        f'{type(self).__name__} declares no unmatched_shapes')
        for http_method, path in self.unmatched_shapes:
            self.assertEqual(self.request(path, method=http_method)[0], 404,
                             f'{http_method} {path}')
