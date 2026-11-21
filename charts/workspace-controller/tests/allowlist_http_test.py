"""The allowlist API's boundary: admin-only, same-origin writes, and no
route at all on the public signup listener or the self-serve listener."""
import http.client
import http.server
import json
import os
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import controller
import signup_allowlist
from signup import handler as signup_handler
from signup_allowlist import RECORD
from signup_allowlist_test import github_lookup
from signup_test import fixture

ADMIN = {'X-Forwarded-Preferred-Username': 'alice', 'X-Forwarded-Email': 'alice@example.test'}
ENDPOINTS = (('GET', '/api/allowlist', None), ('GET', '/api/allowlist/lookup?login=mona', None),
             ('POST', '/api/allowlist', '{"login": "mona", "githubId": "456"}'),
             ('DELETE', '/api/allowlist/456', None))


class AllowlistHTTPTest(unittest.TestCase):
    def setUp(self):
        self.service = fixture()
        self.service.backend.lookup.side_effect = github_lookup
        for name, value in (('SIGNUP_SERVICE', self.service), ('SIGNUP_ENABLED', True),
                            ('TRUSTED_PROXY', True), ('ADMIN_USERS', {'alice'}), ('DEV_TOKEN', 'devtoken')):
            patcher = patch.object(controller, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = self.serve(controller.Handler)
        self.origin = 'http://%s:%d' % self.server.server_address

    def serve(self, handler, restricted=False):
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        server.restricted = restricted
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def call(self, method, path, body=None, headers=None, server=None):
        server = server or self.server
        conn = http.client.HTTPConnection(*server.server_address, timeout=5)
        headers = dict(headers or {})
        if body is not None:
            headers.setdefault('Content-Type', 'application/json')
        conn.request(method, path, body, headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        try:
            return response.status, json.loads(raw)
        except ValueError:
            return response.status, raw

    def write(self, method, path, body=None, **headers):
        return self.call(method, path, body, dict(ADMIN, Origin=self.origin, **headers))

    def test_every_endpoint_requires_an_admin(self):
        identities = ({}, {'X-Forwarded-Preferred-Username': 'bob'},
                      {'X-Forwarded-Groups': '456'}, {'Authorization': 'Bearer wrong'})
        for method, path, body in ENDPOINTS:
            for identity in identities:
                with self.subTest(method=method, path=path, identity=identity):
                    self.assertEqual(self.call(method, path, body, identity)[0], 401)
        self.assertNotIn(RECORD, self.service.store.records)

    def test_cross_site_writes_are_refused_without_a_write(self):
        for origin in ('https://evil.test', 'null', self.origin + '.evil.test'):
            for method, path, body in ENDPOINTS[2:]:
                with self.subTest(origin=origin, method=method):
                    status, payload = self.call(method, path, body, dict(ADMIN, Origin=origin))
                    self.assertEqual((status, payload['code']), (403, 'forbidden'))
        self.assertNotIn(RECORD, self.service.store.records)

    def test_add_needs_a_json_body(self):
        status, payload = self.write('POST', '/api/allowlist', 'login=mona&githubId=456',
                                     **{'Content-Type': 'text/plain'})
        self.assertEqual((status, payload['code']), (400, 'invalid_request'))
        status, payload = self.write('POST', '/api/allowlist', '{not json')
        self.assertEqual((status, payload['code']), (400, 'invalid_request'))
        self.assertNotIn(RECORD, self.service.store.records)

    def test_lookup_add_list_remove_round_trip(self):
        status, account = self.call('GET', '/api/allowlist/lookup?login=mona', headers=ADMIN)
        self.assertEqual((status, account['githubId'], account['allowed']), (200, '456', False))
        status, added = self.write('POST', '/api/allowlist', '{"login": "mona", "githubId": "456", "note": "intern"}')
        self.assertEqual((status, added['created'], added['entry']['addedBy']), (201, True, 'alice'))
        status, again = self.write('POST', '/api/allowlist', '{"login": "mona", "githubId": "456"}')
        self.assertEqual((status, again['created']), (200, False))
        status, overview = self.call('GET', '/api/allowlist', headers=ADMIN)
        self.assertEqual([(e['githubId'], e['source']) for e in overview['entries']],
                         [('456', 'console'), ('123', 'helm')])
        self.assertEqual(self.write('DELETE', '/api/allowlist/456'), (200, {'removed': True}))
        self.assertEqual(self.write('DELETE', '/api/allowlist/456'), (200, {'removed': False}))

    def test_non_browser_admin_without_origin_is_attributed(self):
        headers = {'Authorization': 'Bearer devtoken'}
        status, added = self.call('POST', '/api/allowlist', '{"login": "mona", "githubId": "456"}', headers)
        self.assertEqual((status, added['entry']['addedBy']), (201, 'dev-token'))

    def test_failures_are_short_coded_and_correlated(self):
        for path, status, code in (('/api/allowlist/lookup?login=nobody', 404, 'github_user_not_found'),
                                   ('/api/allowlist/lookup?login=acme', 422, 'not_a_person'),
                                   ('/api/allowlist/lookup?login=', 400, 'invalid_request')):
            with self.subTest(path=path):
                got, payload = self.call('GET', path, headers=ADMIN)
                self.assertEqual((got, payload['code']), (status, code))
                self.assertTrue(payload['error'] and payload['error_id'] and payload['request_id'])
        status, payload = self.write('DELETE', '/api/allowlist/123')
        self.assertEqual((status, payload['code']), (409, 'allowlist_readonly'))
        self.assertEqual(self.write('DELETE', '/api/allowlist/not-an-id')[0], 404)

    def test_unexpected_failure_is_logged_but_not_leaked(self):
        with patch.object(signup_allowlist.AllowlistConsole, 'overview', side_effect=RuntimeError('token=secret')), \
             self.assertLogs('controller', 'INFO') as logs:
            status, payload = self.call('GET', '/api/allowlist', headers=ADMIN)
        self.assertEqual((status, payload['code']), (500, 'internal_error'))
        self.assertNotIn('secret', json.dumps(payload))
        self.assertIn('signup_allowlist_exception', [r.event for r in logs.records if hasattr(r, 'event')])

    def test_routes_do_not_exist_while_public_signup_is_off(self):
        with patch.object(controller, 'SIGNUP_ENABLED', False):
            for method, path, body in ENDPOINTS:
                with self.subTest(method=method):
                    self.assertEqual(self.write(method, path, body)[0], 404)
            self.assertFalse(self.call('GET', '/api/provision/config', headers=ADMIN)[1]['signupEnabled'])
        self.assertTrue(self.call('GET', '/api/provision/config', headers=ADMIN)[1]['signupEnabled'])

    def test_self_serve_listener_never_serves_the_allowlist(self):
        restricted = self.serve(controller.Handler, restricted=True)
        # No body, as for the signup listener below: the restricted guard 404s
        # on the path before reading one.
        for method, path, _ in ENDPOINTS:
            with self.subTest(method=method, path=path):
                self.assertEqual(self.call(method, path, None, ADMIN, server=restricted)[0], 404)
        self.assertNotIn(RECORD, self.service.store.records)

    def test_metrics_name_each_route_once_whatever_the_id(self):
        from observability_http import route_identity
        self.assertEqual([route_identity(p) for p in (
            '/oauth/api/allowlist', '/api/allowlist/lookup?login=x', '/api/allowlist/456', '/api/allowlist/7')],
            ['/api/allowlist', '/api/allowlist/lookup', '/api/allowlist/:id', '/api/allowlist/:id'])

    def test_signup_listener_has_no_allowlist_route(self):
        signup = self.serve(signup_handler(self.service, 'https://signup.example.test', 's' * 32, b'<html></html>'))
        signed_in = dict(ADMIN, **{'X-Forwarded-Groups': '456', 'Cookie': 'session=x',
                                   'Origin': 'https://signup.example.test'})
        # Probe without a body: routing is decided on the path alone, and the
        # signup listener answers an unknown route before reading any body
        # (an unread body can make Windows reset the connection instead).
        for method, path, _ in ENDPOINTS + (('POST', '/oauth/api/allowlist', None),
                                            ('GET', '/oauth/api/allowlist', None)):
            with self.subTest(method=method, path=path):
                status, _ = self.call(method, path, None, signed_in, server=signup)
                self.assertIn(status, (404, 501))   # 501: the signup handler has no DELETE at all
        self.assertNotIn(RECORD, self.service.store.records)


if __name__ == '__main__':
    unittest.main()
