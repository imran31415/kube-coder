"""Tests for the /api/docs endpoints (manifest, page fetch, search).

Boots a real ThreadingHTTPServer on a free port with DOCS_DIR pointed at
a temp directory and check_claude_auth monkey-patched to True so we can
exercise the handlers without setting up OAuth headers. A separate test
asserts the handlers do require auth when the monkey-patch is removed.

Run with:
    cd charts/workspace && python3 -m unittest tests.docs_api_test
"""

import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402


def _free_port():
    import socket
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


MANIFEST = {
    'version': 1,
    'sections': [
        {
            'id': 'tasks',
            'title': 'Tasks',
            'pages': [
                {'id': 'tasks-concepts', 'title': 'Concepts', 'file': 'in-app/tasks-concepts.md', 'summary': 'lifecycle and tmux'},
                {'id': 'tasks-api', 'title': 'HTTP API', 'file': 'claude-task-api.md', 'summary': 'reference'},
            ],
        },
        {
            'id': 'memory',
            'title': 'Memory',
            'pages': [
                {'id': 'memory-concepts', 'title': 'Concepts', 'file': 'in-app/memory-concepts.md', 'summary': 'namespaces'},
            ],
        },
    ],
}


class DocsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix='kc-docs-')
        os.makedirs(os.path.join(cls.tmpdir, 'in-app'))
        with open(os.path.join(cls.tmpdir, '_manifest.json'), 'w') as f:
            json.dump(MANIFEST, f)
        with open(os.path.join(cls.tmpdir, 'in-app', 'tasks-concepts.md'), 'w') as f:
            f.write('# Tasks\n\nLifecycle of a task. The word **needle** appears here.\n')
        with open(os.path.join(cls.tmpdir, 'in-app', 'memory-concepts.md'), 'w') as f:
            f.write('# Memory\n\nNamespaces, importance, tags.\n')
        with open(os.path.join(cls.tmpdir, 'claude-task-api.md'), 'w') as f:
            f.write('# HTTP API\n\nFull reference for the Claude Task API.\n')

        # Point DocsManager at the tmpdir and clear its caches.
        cls._docs_dir_save = server.DocsManager.DOCS_DIR
        server.DocsManager.DOCS_DIR = cls.tmpdir
        server.DocsManager._PAGE_CACHE = {}
        server.DocsManager._MANIFEST_CACHE = (0.0, None)

        # Bypass auth for the dispatch path.
        cls._auth_save = server.BrowserHandler.check_claude_auth
        server.BrowserHandler.check_claude_auth = lambda self: True

        cls.port = _free_port()
        cls.httpd = http.server.ThreadingHTTPServer(
            ('127.0.0.1', cls.port), server.BrowserHandler,
        )
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server.DocsManager.DOCS_DIR = cls._docs_dir_save
        server.BrowserHandler.check_claude_auth = cls._auth_save
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _get(self, path):
        with urllib.request.urlopen(f'http://127.0.0.1:{self.port}{path}', timeout=5) as r:
            body = r.read()
            return r.status, json.loads(body) if body else None

    def test_manifest_lists_sections_and_pages(self):
        status, body = self._get('/api/docs')
        self.assertEqual(status, 200)
        self.assertEqual(body['version'], 1)
        ids = [s['id'] for s in body['sections']]
        self.assertEqual(ids, ['tasks', 'memory'])
        first_pages = [p['id'] for p in body['sections'][0]['pages']]
        self.assertEqual(first_pages, ['tasks-concepts', 'tasks-api'])

    def test_page_returns_markdown_and_metadata(self):
        status, body = self._get('/api/docs/tasks-concepts')
        self.assertEqual(status, 200)
        self.assertEqual(body['id'], 'tasks-concepts')
        self.assertEqual(body['section_id'], 'tasks')
        self.assertIn('Lifecycle of a task', body['markdown'])
        self.assertGreater(body['edited_at'], 0)

    def test_unknown_page_404s(self):
        try:
            self._get('/api/docs/no-such-id')
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)
        else:
            self.fail('Expected 404 for unknown doc id')

    def test_search_ranks_title_above_body(self):
        status, body = self._get('/api/docs/search?q=concepts')
        self.assertEqual(status, 200)
        ids = [r['id'] for r in body['results']]
        self.assertIn('tasks-concepts', ids)
        self.assertIn('memory-concepts', ids)
        # Both titles contain "concepts" but the term doesn't appear in tasks-api
        self.assertNotIn('tasks-api', ids)

    def test_search_finds_body_terms(self):
        status, body = self._get('/api/docs/search?q=needle')
        self.assertEqual(status, 200)
        ids = [r['id'] for r in body['results']]
        self.assertEqual(ids, ['tasks-concepts'])
        self.assertIn('needle', body['results'][0]['snippet'])

    def test_search_empty_query_returns_empty(self):
        status, body = self._get('/api/docs/search?q=')
        self.assertEqual(status, 200)
        self.assertEqual(body['results'], [])


class DocsApiAuthGateTests(unittest.TestCase):
    """Separate suite — boots a fresh server WITHOUT the auth bypass so
    we can prove the handlers do require check_claude_auth() to pass."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix='kc-docs-auth-')
        with open(os.path.join(cls.tmpdir, '_manifest.json'), 'w') as f:
            json.dump({'version': 1, 'sections': []}, f)
        cls._docs_dir_save = server.DocsManager.DOCS_DIR
        server.DocsManager.DOCS_DIR = cls.tmpdir
        server.DocsManager._MANIFEST_CACHE = (0.0, None)
        cls.port = _free_port()
        cls.httpd = http.server.ThreadingHTTPServer(
            ('127.0.0.1', cls.port), server.BrowserHandler,
        )
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server.DocsManager.DOCS_DIR = cls._docs_dir_save
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_manifest_requires_auth(self):
        try:
            with urllib.request.urlopen(
                f'http://127.0.0.1:{self.port}/api/docs', timeout=5
            ) as r:
                self.fail(f'expected 401, got {r.status}')
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)

    def test_page_requires_auth(self):
        try:
            with urllib.request.urlopen(
                f'http://127.0.0.1:{self.port}/api/docs/anything', timeout=5
            ) as r:
                self.fail(f'expected 401, got {r.status}')
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)


if __name__ == '__main__':
    unittest.main()
