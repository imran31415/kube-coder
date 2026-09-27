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
from tests.http_harness import (  # noqa: E402
    DomainEndpointTests, DomainRouteTests, EndpointTestCase,
)


class FilesRouteOrderTests(DomainRouteTests, unittest.TestCase):

    table = files.ROUTES
    foreign_paths = (('GET', '/api/files/'), ('GET', '/api/files/list/deeper'),
                     ('GET', '/api/filesx'))
    oauth_samples = (('GET', '/api/files/list'), ('POST', '/api/files/mkdir'),
                     ('DELETE', '/api/files'))
    # These handlers parse self.path themselves, so no route carries query=;
    # only the DELETE needs its query split off before matching (do_GET hands
    # the table a path whose query is already gone).
    wrong_verb_samples = (('GET', '/api/files/upload'),
                          ('POST', '/api/files/list'),
                          ('GET', '/api/files'),
                          ('DELETE', '/api/files/list'))
    strip_query_handlers = {'handle_file_delete'}

    def test_the_five_reads(self):
        for suffix, handler in (('list', 'handle_files_list'),
                                ('raw', 'handle_file_raw'),
                                ('download', 'handle_file_download'),
                                ('preview', 'handle_file_preview'),
                                ('view', 'handle_file_view')):
            self.assertEqual(self.resolve('GET', '/api/files/' + suffix),
                             handler)

    def test_the_three_writes(self):
        for suffix, handler in (('upload', 'handle_file_upload'),
                                ('mkdir', 'handle_file_mkdir'),
                                ('rename', 'handle_file_rename')):
            self.assertEqual(self.resolve('POST', '/api/files/' + suffix),
                             handler)

    def test_delete_matches_despite_the_query_string(self):
        # do_DELETE routes on the path with its query still attached, so this
        # route only matches at all because of the strip_query column.
        for path in ('/api/files', '/api/files?path=notes.txt'):
            self.assertEqual(self.resolve('DELETE', path),
                             'handle_file_delete', path)


class FilesEndpointBehaviourTests(DomainEndpointTests, EndpointTestCase):
    """Status codes for the migrated routes, over a real server."""

    oauth_reachable = (('GET', '/api/files/list'), ('POST', '/api/files/mkdir'),
                       ('DELETE', '/api/files?path=x'))
    unmatched_shapes = (('GET', '/api/files/list/deeper'),
                        ('POST', '/api/files/list'),
                        ('DELETE', '/api/files/nope'))

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


if __name__ == '__main__':
    unittest.main()
