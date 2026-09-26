"""In-app documentation site: manifest, page fetch, search (#100).

Three GET routes, all auth-gated and all reading `DocsManager` (which still
lives in server.py). The one thing worth spelling out is the ordering hazard
the chain carried implicitly:

  * `/api/docs/search` has to be registered before the `^/api/docs/(<id>)$`
    page route, because `search` is itself a legal page id as far as that
    pattern is concerned. Swap the two and the search endpoint becomes a
    404 for a doc page named "search".

`handle_docs_search` reads the request's query string, which do_GET used to
parse inline and hand over as an argument. That is now the route's
`query=True` column — see handlers/routing.py.
"""

import re
import sys

import handlers
from handlers.routing import RouteTable


class DocsRoutes:
    """BrowserHandler methods backing `ROUTES`."""

    def handle_docs_manifest(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            manifest = handlers.server.DocsManager.load_manifest()
            self.send_json(manifest)
        except Exception as e:
            print(f'[docs] manifest error: {e}', file=sys.stderr)
            self.send_json({'error': str(e), 'code': 'internal'}, 500)

    def handle_docs_page(self, page_id):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            page = handlers.server.DocsManager.get_page(page_id)
            self.send_json(page)
        except KeyError:
            self.send_json({'error': f'Unknown doc page: {page_id}'}, 404)
        except Exception as e:
            print(f'[docs] page {page_id} error: {e}', file=sys.stderr)
            self.send_json({'error': str(e), 'code': 'internal'}, 500)

    def handle_docs_search(self, query):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        q = ''
        try:
            q = (query.get('q', [''])[0] or '').strip()
            try:
                limit = int(query.get('limit', ['25'])[0])
            except (ValueError, TypeError):
                limit = 25
            limit = max(1, min(100, limit))
            results = handlers.server.DocsManager.search(q, limit=limit)
            self.send_json({'q': q, 'results': results})
        except Exception as e:
            print(f'[docs] search {q!r} error: {e}', file=sys.stderr)
            self.send_json({'error': str(e), 'code': 'internal'}, 500)


#: Consulted by do_GET where this domain's three elif branches used to sit.
#: Registration order is match order.
ROUTES = RouteTable()

ROUTES.add('GET', '/api/docs', 'handle_docs_manifest')
# ORDERING HAZARD: `search` also matches the page-id pattern below, so the
# search route has to be registered ahead of it.
ROUTES.add('GET', '/api/docs/search', 'handle_docs_search', query=True)
ROUTES.add('GET', re.compile(r'^/api/docs/([a-zA-Z0-9_-]+)$'), 'handle_docs_page')
