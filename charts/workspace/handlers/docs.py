"""In-app documentation site: manifest, page fetch, search (#100).

Three GET routes, all auth-gated and all reading `DocsManager` (which still
lives in server.py). The one thing worth spelling out is the ordering hazard
the chain carried implicitly:

  * `/api/docs/search` has to be registered before the `^/api/docs/(<id>)$`
    page route, because `search` is itself a legal page id as far as that
    pattern is concerned. Swap the two and the search endpoint becomes a
    404 for a doc page named "search".

`/api/docs/assets/<path>` serves the raster images docs embed with relative
paths (e.g. boards.md's `screenshots/board-processor/*.png`). It is the only
route that returns bytes rather than JSON, so it is restricted to a raster
image allowlist — no SVG, no HTML — because the dashboard is same-origin.

`handle_docs_search` reads the request's query string, which do_GET used to
parse inline and hand over as an argument. That is now the route's
`query=True` column — see handlers/routing.py.
"""
from observability_logging import legacy_log

import os
import re
import sys
import urllib.parse

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
            legacy_log('workspace', f'[docs] manifest error: {e}', file=sys.stderr)
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
            legacy_log('workspace', f'[docs] page {page_id} error: {e}', file=sys.stderr)
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
            legacy_log('workspace', f'[docs] search {q!r} error: {e}', file=sys.stderr)
            self.send_json({'error': str(e), 'code': 'internal'}, 500)

    #: Raster only. SVG can carry script and is same-origin here, so it is
    #: deliberately absent (as is anything that isn't an image).
    ASSET_MIME = {
        '.png': 'image/png',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.gif': 'image/gif',
        '.webp': 'image/webp',
    }

    def handle_docs_asset(self, rel):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        rel = urllib.parse.unquote(rel)
        ctype = self.ASSET_MIME.get(os.path.splitext(rel)[1].lower())
        if not ctype:
            self.send_json({'error': 'only png/jpeg/gif/webp images are served'}, 415)
            return
        try:
            target = handlers.server.DocsManager._safe_join(rel)
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        if not os.path.isfile(target):
            self.send_json({'error': 'not found'}, 404)
            return
        try:
            with open(target, 'rb') as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Cache-Control', 'private, max-age=300')
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError as e:
            legacy_log('workspace', f'[docs] asset {rel!r} error: {e}', file=sys.stderr)
            self.send_json({'error': 'read failed'}, 500)


#: Consulted by do_GET where this domain's three elif branches used to sit.
#: Registration order is match order.
ROUTES = RouteTable()

ROUTES.add('GET', '/api/docs', 'handle_docs_manifest')
# ORDERING HAZARD: `search` also matches the page-id pattern below, so the
# search route has to be registered ahead of it.
ROUTES.add('GET', '/api/docs/search', 'handle_docs_search', query=True)
ROUTES.add('GET', re.compile(r'^/api/docs/([a-zA-Z0-9_-]+)$'), 'handle_docs_page')
# Has a slash after `assets`, so it can never collide with the page-id route.
ROUTES.add('GET', re.compile(r'^/api/docs/assets/(.+)$'), 'handle_docs_asset')
