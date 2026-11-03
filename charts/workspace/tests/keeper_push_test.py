"""What the workspace pushes to the keeper, and what it must never push.

The property worth most of this file is the second one. The keeper is reachable
from the ingress controller and holds the authority to start a pod; the whole
design rests on it holding no secret, so "a secret cannot leave in a push" has
to be a property of the code and not a promise about it.

That is tested twice, deliberately differently:

  * by name — every secret field the three trigger managers write is planted in
    a fixture and looked for in the bytes that would go on the wire;
  * by shape — every pushed entry's key set must be a subset of the three keys
    the keeper is allowed to learn. This one catches a field added to a trigger
    config next year that nobody thought to add to the list above.

Run:  cd charts/workspace && python3 -m unittest tests.keeper_push_test
"""

import hashlib
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import keeper_push  # noqa: E402

# The only keys the keeper is ever told about a trigger.
ALLOWED_KEYS = {'kind', 'id', 'provider'}

# Every secret field the three managers write, as of server.py's
# WebhookManager / CronManager / PageWatchManager. Planted in the fixtures and
# then hunted for in the push body.
SECRET_VALUES = {
    'hmac_secret': 'top-secret-hmac',
    'fire_token': 'top-secret-fire-token',
    'response_secret': 'top-secret-response',
}

# Not secrets, but still none of the keeper's business, and each one is a thing
# a careless projection would carry along.
PRIVATE_VALUES = {
    'prompt_template': 'Review {{ payload.pull_request.title }} in secret-project',
    'workdir': '/home/dev/secret-project',
    'url': 'https://internal.example.com/private-page',
    'response_url': 'https://internal.example.com/callback',
}


class _Recorder:
    """Stands in for safe_http.fetch and keeps what it was handed."""

    def __init__(self, status=200):
        self.status = status
        self.calls = []

    def __call__(self, url, *, method='GET', headers=None, body=None,
                 timeout=30, max_bytes=None, allow_internal=False):
        self.calls.append({
            'url': url, 'method': method, 'headers': dict(headers or {}),
            'body': body, 'timeout': timeout, 'allow_internal': allow_internal,
        })
        return self.status, {}, b''


class KeeperPushTestCase(unittest.TestCase):
    """A workspace with one of each kind of trigger, all fully populated."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.webhooks = os.path.join(self.tmp, 'webhooks')
        self.crons = os.path.join(self.tmp, 'crons')
        self.watches = os.path.join(self.tmp, 'page-watches')
        for d in (self.webhooks, self.crons, self.watches):
            os.makedirs(d)

        self._write(self.webhooks, 'gh-pr', {
            'id': 'gh-pr', 'provider': 'github',
            'signature_header': 'X-Hub-Signature-256', 'created_at': 1,
            'hmac_secret': SECRET_VALUES['hmac_secret'],
            'response_secret': SECRET_VALUES['response_secret'],
            'prompt_template': PRIVATE_VALUES['prompt_template'],
            'workdir': PRIVATE_VALUES['workdir'],
            'response_url': PRIVATE_VALUES['response_url'],
        })
        self._write(self.crons, 'nightly', {
            'id': 'nightly', 'schedule': '0 2 * * *', 'timezone': 'UTC',
            'fire_token': SECRET_VALUES['fire_token'],
            'response_secret': SECRET_VALUES['response_secret'],
            'prompt_template': PRIVATE_VALUES['prompt_template'],
            'workdir': PRIVATE_VALUES['workdir'],
        })
        self._write(self.watches, 'docs', {
            'id': 'docs', 'url': PRIVATE_VALUES['url'], 'selector': '#main',
            'fire_token': SECRET_VALUES['fire_token'],
            'prompt_template': PRIVATE_VALUES['prompt_template'],
        })

        # Configured, but every request is captured rather than sent.
        os.environ['KEEPER_CONTROL_URL'] = 'http://keeper-test-control.ws-test.svc.cluster.local'
        os.environ['KEEPER_CONTROL_SECRET'] = 'the-shared-secret'
        self.addCleanup(os.environ.pop, 'KEEPER_CONTROL_URL', None)
        self.addCleanup(os.environ.pop, 'KEEPER_CONTROL_SECRET', None)
        # `keeper_push.safe_http` is the shared safe_http module object, so this
        # patch is global for the life of the test. Register the restore BEFORE
        # applying it, so the module cannot stay patched if anything between
        # here and the end of setUp raises — a leaked patch would silently stub
        # out every other suite's outbound HTTP.
        self.rec = _Recorder()
        self._real_fetch = keeper_push.safe_http.fetch
        self.addCleanup(self._restore)
        keeper_push.safe_http.fetch = self.rec
        # Registered here, where the value is still the original one: a test
        # that lowers it to keep itself quick must not leave it lowered for
        # everything that runs after.
        self.addCleanup(setattr, keeper_push, 'MAX_ATTEMPTS', keeper_push.MAX_ATTEMPTS)

    def _restore(self):
        keeper_push.safe_http.fetch = self._real_fetch

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _write(directory, tid, cfg):
        with open(os.path.join(directory, tid + '.json'), 'w') as f:
            json.dump(cfg, f)

    def _collect(self):
        return keeper_push.collect_triggers(self.webhooks, self.crons, self.watches)

    def _push(self):
        ok = keeper_push.KeeperPush.push_triggers_now(self.webhooks, self.crons, self.watches)
        self.assertTrue(ok, 'the push should have succeeded')
        self.assertEqual(len(self.rec.calls), 1)
        return self.rec.calls[0]


class CollectTests(KeeperPushTestCase):

    def test_it_finds_every_kind(self):
        got = {(t['kind'], t['id']) for t in self._collect()}
        self.assertEqual(got, {
            ('webhook', 'gh-pr'), ('cron', 'nightly'), ('page-watch', 'docs')})

    def test_it_carries_the_webhook_provider(self):
        by_id = {t['id']: t for t in self._collect()}
        self.assertEqual(by_id['gh-pr'].get('provider'), 'github')
        # Crons and page-watches have no provider; the key is simply absent
        # rather than empty, so the keeper's JSON stays the shape it declares.
        self.assertNotIn('provider', by_id['nightly'])
        self.assertNotIn('provider', by_id['docs'])

    def test_an_unknown_provider_is_dropped_not_forwarded(self):
        self._write(self.webhooks, 'odd', {'id': 'odd', 'provider': 'not-a-provider'})
        by_id = {t['id']: t for t in self._collect()}
        self.assertIn('odd', by_id)
        self.assertNotIn('provider', by_id['odd'],
                         'an unrecognised provider must not be passed on')

    def test_a_malformed_config_still_yields_its_id(self):
        # The id comes from the filename, so a config that cannot be parsed
        # still produces a known id: the trigger exists, and the keeper must
        # not start 404ing a real webhook because its file got mangled.
        with open(os.path.join(self.webhooks, 'broken.json'), 'w') as f:
            f.write('{not json')
        by_id = {t['id']: t for t in self._collect()}
        self.assertIn('broken', by_id)
        self.assertNotIn('provider', by_id['broken'])

    def test_a_missing_directory_yields_nothing(self):
        # Fail safe: nothing known means the keeper answers 404 and wakes
        # nothing, never "allow everything".
        got = keeper_push.collect_triggers(
            os.path.join(self.tmp, 'nope'), self.crons, self.watches)
        self.assertEqual([t['id'] for t in got], ['nightly', 'docs'])

    def test_it_ignores_non_json_files(self):
        open(os.path.join(self.crons, 'README.txt'), 'w').close()
        open(os.path.join(self.crons, '.hidden'), 'w').close()
        self.assertEqual(
            sorted(t['id'] for t in self._collect()), ['docs', 'gh-pr', 'nightly'])


class NoSecretLeavesTests(KeeperPushTestCase):
    """The file's reason for existing."""

    def test_no_secret_value_appears_in_the_push(self):
        call = self._push()
        blob = call['body'].decode('utf-8')
        for name, value in SECRET_VALUES.items():
            self.assertNotIn(value, blob, f'{name} reached the keeper')

    def test_nothing_private_appears_in_the_push(self):
        call = self._push()
        blob = call['body'].decode('utf-8')
        for name, value in PRIVATE_VALUES.items():
            self.assertNotIn(value, blob, f'{name} reached the keeper')

    def test_no_secret_field_name_appears_either(self):
        # Not just the values: a key called hmac_secret with an empty value
        # would still mean the projection is reading the wrong things.
        blob = self._push()['body'].decode('utf-8')
        for name in list(SECRET_VALUES) + list(PRIVATE_VALUES):
            self.assertNotIn(name, blob, f'the field name {name} reached the keeper')

    def test_every_entry_has_only_the_allowed_keys(self):
        """The structural guarantee.

        Checking known field names catches today's secrets. This catches
        tomorrow's: any key added to a trigger config that the projection
        starts carrying fails here, whatever it is called.
        """
        payload = json.loads(self._push()['body'].decode('utf-8'))
        self.assertEqual(set(payload), {'triggers'})
        for entry in payload['triggers']:
            extra = set(entry) - ALLOWED_KEYS
            self.assertEqual(extra, set(), f'{entry} carries keys the keeper must not learn: {extra}')

    def test_the_token_itself_never_travels_only_its_hash(self):
        token = 'tok-the-real-api-token'
        self.assertTrue(keeper_push.KeeperPush.push_token_hash_now(token))
        blob = self.rec.calls[0]['body'].decode('utf-8')
        self.assertNotIn(token, blob, 'the API token itself was sent')
        self.assertIn(hashlib.sha256(token.encode()).hexdigest(), blob)

    def test_token_hash_is_plain_sha256(self):
        # Computed independently of the module, so an encoding slip shows up.
        self.assertEqual(
            keeper_push.token_hash('abc'),
            hashlib.sha256(b'abc').hexdigest())


class TransportTests(KeeperPushTestCase):

    def test_the_request_is_addressed_and_authenticated(self):
        call = self._push()
        self.assertEqual(call['method'], 'POST')
        self.assertTrue(call['url'].endswith('/api/keeper/triggers'), call['url'])
        self.assertEqual(call['headers'].get('Authorization'), 'Bearer the-shared-secret')
        self.assertEqual(call['headers'].get('Content-Type'), 'application/json')

    def test_it_allows_the_cluster_ip_explicitly(self):
        # The keeper is a ClusterIP, which safe_http refuses by default. The
        # exception has to be asked for by this call, not inherited from the
        # ALLOW_INTERNAL_HOOKS global, which is about user-supplied URLs.
        self.assertTrue(self._push()['allow_internal'])

    def test_it_does_nothing_without_a_keeper(self):
        del os.environ['KEEPER_CONTROL_URL']
        self.assertFalse(keeper_push.KeeperPush.enabled())
        self.assertFalse(
            keeper_push.KeeperPush.push_triggers_now(self.webhooks, self.crons, self.watches))
        self.assertEqual(self.rec.calls, [], 'a keeper-less workspace must send nothing')

    def test_it_does_nothing_without_a_secret(self):
        del os.environ['KEEPER_CONTROL_SECRET']
        self.assertFalse(keeper_push.KeeperPush.enabled())
        self.assertEqual(self.rec.calls, [])

    def test_a_refusal_is_not_retried(self):
        # The keeper rejecting the content will reject it again; retrying would
        # only delay the log line that says why.
        self.rec.status = 400
        self.assertFalse(
            keeper_push.KeeperPush.push_triggers_now(self.webhooks, self.crons, self.watches))
        self.assertEqual(len(self.rec.calls), 1)

    def test_a_server_error_is_retried_then_given_up_on(self):
        # A keeper mid-restart is the case worth retrying for.
        self.rec.status = 503
        keeper_push.MAX_ATTEMPTS = 2  # keep the test quick
        self.assertFalse(
            keeper_push.KeeperPush.push_triggers_now(self.webhooks, self.crons, self.watches))
        self.assertEqual(len(self.rec.calls), 2)

    def test_a_transport_failure_never_raises_into_the_caller(self):
        # A push happens on the trigger-edit path; a keeper that is down must
        # not turn into a failed save.
        def boom(*a, **kw):
            raise OSError('connection refused')

        keeper_push.safe_http.fetch = boom
        keeper_push.MAX_ATTEMPTS = 1
        self.assertFalse(
            keeper_push.KeeperPush.push_triggers_now(self.webhooks, self.crons, self.watches))

    def test_a_nonsense_url_is_refused_rather_than_fetched(self):
        os.environ['KEEPER_CONTROL_URL'] = 'not-a-url'
        self.assertFalse(
            keeper_push.KeeperPush.push_triggers_now(self.webhooks, self.crons, self.watches))
        self.assertEqual(self.rec.calls, [])


if __name__ == '__main__':
    unittest.main()
