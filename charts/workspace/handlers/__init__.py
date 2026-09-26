"""Per-domain HTTP handlers for server.py's BrowserHandler (issue #100).

server.py grew into one file that mixes static serving, SPA proxying and ~15
API surfaces, with routing spread across if/elif chains in do_GET / do_POST /
do_DELETE. This package is where those surfaces move, one domain at a time:
each module owns an ordered `RouteTable` (see `handlers.routing`) plus a mixin
carrying the handler methods that table names.

## Why `bind()` instead of `import server`

A module in here must never `import server`. In the pod the backend is started
as `python3 server.py`, so the module is `__main__`; the test suite does
`import server`, so there it is `server`. An `import server` from a handler
module would therefore execute a *second* copy of the file under a second
name — duplicate manager classes, duplicate module globals, duplicate
background threads. (`boards/` avoids the same trap by never importing server
at all and taking a callable from BoardsManager instead.)

So server.py hands this package its own module object once, at import time,
and handler modules reach the managers that still live there as
`handlers.server.MetricsCollector`, `handlers.server.GitHubManager`, ….
As those managers move out of server.py the references go with them, and the
last one to leave takes `bind` with it.
"""

#: The server.py module object, injected by `bind()` below. None until then.
server = None


def bind(module):
    """Give the handler modules access to server.py's module globals."""
    global server
    server = module
