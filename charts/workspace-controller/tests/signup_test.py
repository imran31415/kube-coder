"""Onboarding regression tests. No real credentials or network required."""
import contextlib
import copy
import http.client
import http.server
import json
import os
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from provision_store import Conflict
from provisioning import Provisioning
from signup_allowlist import Allowlist
from signup import handler, identity, signup_html
from signup_errors import SignupError


class MemoryStore:
    def __init__(self):
        self.records, self.secrets = {}, {}
        self.lock = threading.RLock()

    def get(self, name):
        with self.lock:
            return copy.deepcopy(self.records.get(name))

    def save(self, name, value):
        with self.lock:
            current = self.records.get(name)
            if (current or {}).get('_version') != value.get('_version'):
                raise Conflict()
            result = dict(copy.deepcopy(value), _version=str(int((current or {}).get('_version', '0')) + 1))
            self.records[name] = result
            return copy.deepcopy(result)

    def credentials(self, owner, factory):
        with self.lock:
            if owner not in self.secrets:
                self.secrets[owner] = factory()
            return self.secrets[owner]

    def operations(self):
        return [(name, self.get(name)) for name in list(self.records) if name.startswith('owner-')]

    @contextlib.contextmanager
    def leader(self):
        with self.lock:
            yield True


def fixture():
    store, dex, backend = MemoryStore(), Mock(), Mock()
    backend.lookup.return_value = {'id': 123, 'login': 'octocat', 'type': 'User'}
    backend.valid_slug.return_value = True
    backend.occupied.return_value = False
    backend.publish.return_value = 'a' * 40
    backend.ready.return_value = True
    backend.absent.return_value = True
    backend.job_failed.return_value = False
    return Provisioning(store, dex, backend, 'example.test', Allowlist(store, {'123'}))


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.service = fixture()

    def test_signup_refuses_placeholder_or_legacy_ui(self):
        for html in (b'SPA not built', b'<head></head><body>old console</body>'):
            with self.assertRaisesRegex(ValueError, 'make controller-web'):
                signup_html(html)

    def test_signup_mode_is_injected_into_built_html_once(self):
        result = signup_html(b'<head></head><script>kc-signup</script>')
        self.assertEqual(result.count(b'<meta name="kc-signup"'), 1)

    def test_unapproved_identity_has_no_side_effects(self):
        with self.assertRaises(SignupError) as caught:
            self.service.create('999', 'other')
        self.assertEqual(caught.exception.code, 'not_eligible')
        self.assertEqual(self.service.store.records, {})
        self.service.backend.lookup.assert_not_called()

    def test_verified_id_must_match_github_lookup(self):
        self.service.backend.lookup.return_value['id'] = 999
        with self.assertRaises(SignupError):
            self.service.create('123', 'octocat')
        self.assertFalse(self.service.store.records)

    def test_two_replicas_double_submission_create_one_operation(self):
        other = Provisioning(self.service.store, self.service.dex, self.service.backend, 'example.test', Allowlist(self.service.store, {'123'}))
        results = []
        threads = [threading.Thread(target=lambda s=s: results.append(s.create('123', 'octocat'))) for s in (self.service, other)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(len({r['operationId'] for r in results}), 1)

    def test_resume_after_every_stage_reuses_credentials(self):
        self.service.create('123', 'octocat')
        for stage in ('configuration', 'deployment', 'readiness', 'ready'):
            self.service.tick()
            self.assertEqual(self.service.get('123')['stage'], stage)
            self.service = Provisioning(self.service.store, self.service.dex, self.service.backend, 'example.test', Allowlist(self.service.store, {'123'}))
        self.service.dex.ensure.assert_called_once()
        self.service.backend.deploy.assert_called_once()
        self.assertEqual(self.service.public(self.service.get('123'))['url'], 'https://octocat.example.test/')

    def test_dex_failure_is_safe_and_retryable(self):
        self.service.dex.ensure.side_effect = SignupError('dex_unavailable')
        self.service.create('123', 'octocat')
        self.service.tick()
        record = self.service.get('123')
        self.assertEqual(record['stage'], 'failed')
        self.assertTrue(record['error']['retryable'])
        self.service.backend.publish.assert_not_called()
        record['updatedAt'] -= 20
        self.service.store.save('owner-123', record)
        secret = self.service.store.secrets['123'].copy()
        self.service.dex.ensure.side_effect = None
        self.service.retry('123')
        self.service.tick()
        self.assertEqual(self.service.store.secrets['123'], secret)

    def test_existing_username_cannot_be_claimed(self):
        self.service.backend.occupied.return_value = True
        with self.assertRaises(SignupError) as caught:
            self.service.create('123', 'octocat')
        self.assertEqual(caught.exception.code, 'owner_conflict')

    def test_migration_is_admin_only(self):
        with self.assertRaises(SignupError):
            self.service.create('123', 'octocat', migrate=True)
        self.service.backend.validate_migration.assert_not_called()
        record = self.service.create('123', 'octocat', admin=True, migrate=True)
        self.assertTrue(record['migrate'])
        self.service.backend.validate_migration.assert_called_once_with('octocat')

    def test_interrupted_migration_keeps_preserving_existing_configuration(self):
        self.service.store.save('slug-octocat', {'owner': '123', 'migrate': True})
        record = self.service.create('123', 'octocat')
        self.assertTrue(record['migrate'])

    def test_username_change_keeps_assigned_slug(self):
        original = self.service.create('123', 'octocat')
        self.assertEqual(self.service.create('123', 'renamed')['slug'], original['slug'])

    def test_lost_job_response_retries_same_attempt(self):
        self.service.create('123', 'octocat')
        self.service.tick(); self.service.tick()
        self.service.backend.deploy.side_effect = SignupError('deploy_failed')
        self.service.tick()
        record = self.service.get('123')
        record['updatedAt'] -= 20
        self.service.store.save('owner-123', record)
        self.assertEqual(self.service.retry('123')['attempt'], 1)

    def test_missing_job_retry_returns_to_deployment(self):
        self.service.create('123', 'octocat')
        for _ in range(3): self.service.tick()
        self.service.backend.ready.side_effect = SignupError('deploy_failed')
        self.service.tick()
        record = self.service.get('123')
        record['updatedAt'] -= 20
        self.service.store.save('owner-123', record)
        self.service.backend.job.return_value = None
        retried = self.service.retry('123')
        self.assertEqual(retried['stage'], 'deployment')
        self.assertEqual(retried['attempt'], 1)
        self.service.tick()
        self.assertEqual(self.service.backend.deploy.call_count, 2)

    def test_failed_job_retry_uses_new_attempt_and_cooldown(self):
        self.service.create('123', 'octocat')
        for _ in range(3): self.service.tick()
        self.service.backend.ready.side_effect = SignupError('deploy_failed')
        self.service.tick()
        with self.assertRaises(SignupError) as caught:
            self.service.retry('123')
        self.assertEqual(caught.exception.code, 'busy')
        record = self.service.get('123')
        record['updatedAt'] -= 20
        self.service.store.save('owner-123', record)
        self.service.backend.job_failed.return_value = True
        retried = self.service.retry('123')
        self.assertEqual((retried['stage'], retried['attempt']), ('deployment', 2))

    def test_removed_eligibility_cannot_retry_but_can_observe(self):
        self.service.create('123', 'octocat')
        self.service.allowlist = Allowlist(self.service.store, ())
        with self.assertRaises(SignupError): self.service.retry('123')
        self.assertIsNotNone(self.service.public(self.service.get('123')))

    def test_cleanup_requires_two_absence_observations_and_never_treats_error_as_absence(self):
        self.service.create('123', 'octocat')
        for _ in range(4): self.service.tick()
        def age():
            record = self.service.get('123')
            record['updatedAt'] -= 61
            self.service.store.save('owner-123', record)
        age()
        self.service.backend.deleted.side_effect = SignupError('state_unavailable')
        with self.assertRaises(SignupError): self.service.tick()
        self.service.dex.delete.assert_not_called()
        self.service.backend.deleted.side_effect = None
        self.service.backend.deleted.return_value = True
        self.service.tick()
        self.service.dex.delete.assert_not_called()
        age()
        record = self.service.get('123')
        record['missingSince'] -= 61
        self.service.store.save('owner-123', record)
        self.service.tick()
        self.assertEqual(self.service.get('123')['stage'], 'deleted')
        self.service.dex.delete.assert_called_once_with('workspace-123')

    def test_worker_replays_external_effect_after_state_save_loss(self):
        self.service.create('123', 'octocat')
        with patch.object(self.service.store, 'save', side_effect=SignupError('state_unavailable')):
            with self.assertRaises(SignupError): self.service.tick()
        self.service.tick()
        self.assertEqual(self.service.dex.ensure.call_count, 2)
        self.assertEqual(self.service.dex.ensure.call_args_list[0], self.service.dex.ensure.call_args_list[1])
        self.assertEqual(self.service.get('123')['stage'], 'configuration')

    def test_cleanup_never_deletes_live_workspace_identity(self):
        self.service.create('123', 'octocat')
        for _ in range(4): self.service.tick()
        self.service.backend.absent.return_value = False
        with self.assertRaises(SignupError): self.service.cleanup('octocat')
        self.service.dex.delete.assert_not_called()

    def test_cleanup_idempotent_and_retains_ownership(self):
        self.service.create('123', 'octocat')
        for _ in range(4): self.service.tick()
        self.service.cleanup('octocat'); self.service.cleanup('octocat')
        self.assertEqual(self.service.get('123')['stage'], 'deleted')
        self.assertEqual(self.service.store.get('slug-octocat')['owner'], '123')

    def test_missing_claims_and_multiple_ids_denied(self):
        for value in ('', '123,999', 'octocat', '0', '-1'):
            with self.assertRaises(SignupError):
                identity({'X-Forwarded-Groups': value, 'X-Forwarded-Preferred-Username': 'octocat'})

    def test_incomplete_readiness_times_out_without_open_link(self):
        self.service.create('123', 'octocat')
        for _ in range(3): self.service.tick()
        record = self.service.get('123')
        record['startedAt'] -= 1300
        self.service.store.save('owner-123', record)
        self.service.backend.ready.return_value = False
        self.service.tick()
        result = self.service.public(self.service.get('123'))
        self.assertEqual(result['error']['code'], 'readiness_timeout')
        self.assertIsNone(result['url'])


class SignupHTTPTest(unittest.TestCase):
    def setUp(self):
        self.service = fixture()
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0),
            handler(self.service, 'https://signup.example.test', 's' * 32, b'<html>signup</html>'))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.headers = {'X-Forwarded-Groups': '123', 'X-Forwarded-Preferred-Username': 'octocat', 'Cookie': 'session=test'}

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=20)
        conn.request(method, path, body, headers or {})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, json.loads(data)

    def test_admin_routes_are_not_dispatched_even_with_identity(self):
        for path in ('/api/workspaces', '/api/provision/create', '/oauth/api/workspaces', '/api/admin/token'):
            self.assertEqual(self.request('GET', path, headers=self.headers)[0], 404)

    def test_session_and_csrf_required(self):
        self.assertEqual(self.request('GET', '/api/signup/me')[0], 401)
        self.assertEqual(self.request('POST', '/api/signup/workspace', '{}', self.headers)[0], 403)

    def test_csrf_is_bound_to_session_and_origin(self):
        _, me = self.request('GET', '/api/signup/me', headers=self.headers)
        headers = dict(self.headers, Origin='https://signup.example.test', **{'X-CSRF-Token': me['csrfToken']})
        for override in ({'Origin': 'https://attacker.test'}, {'Cookie': 'session=other'}, {'X-Forwarded-Groups': '456'}):
            with self.subTest(override=override):
                self.assertEqual(self.request('POST', '/api/signup/workspace', '{}', headers | override)[0], 403)
        self.assertFalse(self.service.store.records)

    def test_malformed_oversized_and_forged_payloads_have_no_effects(self):
        _, me = self.request('GET', '/api/signup/me', headers=self.headers)
        headers = dict(self.headers, Origin='https://signup.example.test', **{'X-CSRF-Token': me['csrfToken']})
        for body in ('{', '[]', 'null', '"string"', '{"migrate":true}', '{"user":"other"}', ' ' * 1025):
            with self.subTest(body=body[:20]):
                self.assertEqual(self.request('POST', '/api/signup/workspace', body, headers)[0], 400)
        self.assertFalse(self.service.store.records)

    def test_unauthorized_and_unapproved_requests_never_create(self):
        other = self.headers | {'X-Forwarded-Groups': '999'}
        _, me = self.request('GET', '/api/signup/me', headers=other)
        self.assertFalse(me['eligible'])
        headers = other | {'Origin': 'https://signup.example.test', 'X-CSRF-Token': me['csrfToken']}
        self.assertEqual(self.request('POST', '/api/signup/workspace', '{}', headers)[0], 403)
        self.assertFalse(self.service.store.records)

    def test_own_create_status_flow_and_untrusted_body_rejected(self):
        status, me = self.request('GET', '/api/signup/me', headers=self.headers)
        self.assertEqual(status, 200)
        headers = dict(self.headers, Origin='https://signup.example.test', **{'X-CSRF-Token': me['csrfToken']})
        self.assertEqual(self.request('POST', '/api/signup/workspace', '{"owner":"999"}', headers)[0], 400)
        self.assertEqual(self.request('POST', '/api/signup/workspace', '{}', headers)[0], 202)
        for _ in range(4): self.service.tick()
        _, status = self.request('GET', '/api/signup/status', headers=self.headers)
        self.assertEqual(status['operation']['stage'], 'ready')
        other = dict(self.headers, **{'X-Forwarded-Groups': '999'})
        self.assertIsNone(self.request('GET', '/api/signup/status', headers=other)[1]['operation'])


if __name__ == '__main__':
    unittest.main()
