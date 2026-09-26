"""Tests for the files route table that replaced the elif branches (#100).

The first migrated domain spanning three verbs, and the one that brought the
query string into the table. Two layers, mirroring tests/routing_test.py:
  * The table — which verb owns which path, and the `DELETE /api/files`
    route that only matches because its query string is stripped first.
  * End-to-end over a real server — a 401 proves the route matched at all,
    including with the `?path=` every one of these endpoints carries.

The handlers' own behaviour — traversal confinement, the public-demo
confidentiality gate, upload limits, preview truncation — is covered by
tests/files_api_test.py and is unchanged by the split.

Run with:
    cd charts/workspace && python3 -m unittest tests.files_routes_test
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402
from handlers import files  # noqa: E402
from tests.http_harness import EndpointTestCase, handler_for  # noqa: E402


class FilesRouteOrderTests(unittest.TestCase):

    def _handler_for(self, http_method, path, raw=None):
        return handler_for(files.ROUTES, http_method, path, raw)

    def test_the_five_reads(self):
        for suffix, handler in (('list', 'handle_files_list'),
                                ('raw', 'handle_file_raw'),
                                ('download', 'handle_file_download'),
                                ('preview', 'handle_file_preview'),
                                ('view', 'handle_file_view')):
            self.assertEqual(self._handler_for('GET', '/api/files/' + suffix),
                             handler)

    def test_the_three_writes(self):
        for suffix, handler in (('upload', 'handle_file_upload'),
                                ('mkdir', 'handle_file_mkdir'),
                                ('rename', 'handle_file_rename')):
            self.assertEqual(self._handler_for('POST', '/api/files/' + suffix),
                             handler)

    def test_delete_matches_despite_the_query_string(self):
        # do_DELETE routes on the path with its query still attached, so this
        # route only matches at all because of the strip_query column.
        for path in ('/api/files', '/api/files?path=notes.txt'):
            self.assertEqual(self._handler_for('DELETE', path),
                             'handle_file_delete', path)

    def test_only_the_delete_route_strips_the_query(self):
        # do_GET hands the table a path whose query is already gone, so none
        # of the reads need the column.
        stripping = {r.handler for r in files.ROUTES.routes if r.strip_query}
        self.assertEqual(stripping, {'handle_file_delete'})

    def test_the_verb_is_part_of_the_match(self):
        self.assertIsNone(self._handler_for('GET', '/api/files/upload'))
        self.assertIsNone(self._handler_for('POST', '/api/files/list'))
        self.assertIsNone(self._handler_for('GET', '/api/files'))
        self.assertIsNone(self._handler_for('DELETE', '/api/files/list'))

    def test_paths_outside_the_domain_do_not_match(self):
        for path in ('/api/files/', '/api/files/list/deeper', '/api/filesx'):
            self.assertIsNone(self._handler_for('GET', path), path)

    def test_every_route_matches_the_normalized_path(self):
        for http_method, path in (('GET', '/api/files/list'),
                                  ('POST', '/api/files/mkdir'),
                                  ('DELETE', '/api/files')):
            self.assertIsNotNone(
                self._handler_for(http_method, path, raw='/oauth' + path), path)

    def test_no_route_reads_the_parsed_query(self):
        # These handlers parse self.path themselves; the column would be a
        # signature change, not a relocation.
        self.assertEqual([r.handler for r in files.ROUTES.routes if r.query], [])

    def test_every_route_names_a_real_handler_method(self):
        for route in files.ROUTES.routes:
            self.assertTrue(hasattr(server.BrowserHandler, route.handler),
                            f'{route} names a method BrowserHandler lacks')


class FilesEndpointBehaviourTests(EndpointTestCase):
    """Status codes for the migrated routes, over a real server."""

    def test_reads_are_auth_gated(self):
        # 401 rather than 404 is also the proof the route matched at all.
        for suffix in ('list', 'raw', 'download', 'preview', 'view'):
            path = f'/api/files/{suffix}?path=notes.txt'
            self.assertEqual(self.get(path)[0], 401, path)

    def test_writes_are_auth_gated(self):
        for suffix in ('upload', 'mkdir', 'rename'):
            self.assertEqual(self.post(f'/api/files/{suffix}')[0], 401, suffix)

    def test_delete_is_auth_gated_with_its_query_string_attached(self):
        # The regression the strip_query column guards: without it this is a
        # 404 from the do_DELETE fall-through rather than the handler's 401.
        self.assertEqual(
            self.request('/api/files?path=notes.txt', method='DELETE')[0], 401)

    def test_reachable_under_the_oauth_prefix(self):
        self.assertEqual(self.get('/oauth/api/files/list')[0], 401)
        self.assertEqual(self.post('/oauth/api/files/mkdir')[0], 401)
        self.assertEqual(
            self.request('/oauth/api/files?path=x', method='DELETE')[0], 401)

    def test_unmatched_shapes_stay_404(self):
        self.assertEqual(self.get('/api/files/list/deeper')[0], 404)
        self.assertEqual(self.post('/api/files/list')[0], 404)
        self.assertEqual(self.request('/api/files/nope', method='DELETE')[0], 404)


if __name__ == '__main__':
    unittest.main()
