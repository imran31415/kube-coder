"""Tests for the scanner's model connection and first-use install (#726).

The whole point of this module is that kube-coder writes the scanner's own
config file and never keeps a copy of the key, so most of these tests are
about what does NOT happen: the key never appears in a response, a save never
discards settings made from the terminal, and no probe is ever run by the
suite — the two classes that shell out are injected.

HOME is redirected in setUp, so nothing here reads or writes the real
`~/.strix` of whoever is running the tests.

Run with:
    cd charts/workspace && python3 -m unittest tests.strix_connection_test
"""

import json
import os
import stat
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import strix_connection  # noqa: E402
from tests.envassert import assert_env_lacks_all  # noqa: E402

SECRET = 'sk-test-0123456789abcdef'


class ConnectionTestCase(unittest.TestCase):
    """Each test gets a private HOME, so the module's paths point at a temp."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = tmp.name
        previous = os.environ.get('KC_STRIX_HOME')
        os.environ['KC_STRIX_HOME'] = self.home

        def restore():
            if previous is None:
                os.environ.pop('KC_STRIX_HOME', None)
            else:
                os.environ['KC_STRIX_HOME'] = previous
        self.addCleanup(restore)
        self.mod = strix_connection
        self.addCleanup(setattr, strix_connection, '_install_thread', None)


class SaveTests(ConnectionTestCase):

    def test_saving_writes_the_scanners_own_file_and_format(self):
        self.mod.save_connection(model='prov/model', api_key=SECRET)
        with open(self.mod.config_path(), encoding='utf-8') as f:
            data = json.load(f)
        self.assertEqual(data['env']['STRIX_LLM'], 'prov/model')
        self.assertEqual(data['env']['LLM_API_KEY'], SECRET)

    def test_the_config_file_is_readable_only_by_its_owner(self):
        self.mod.save_connection(model='prov/model', api_key=SECRET)
        mode = stat.S_IMODE(os.stat(self.mod.config_path()).st_mode)
        self.assertEqual(mode & 0o077, 0)

    def test_saving_preserves_settings_made_from_the_terminal(self):
        """A dashboard save must not silently discard the user's own options."""
        os.makedirs(self.mod.config_dir(), exist_ok=True)
        with open(self.mod.config_path(), 'w', encoding='utf-8') as f:
            json.dump({'env': {'STRIX_REASONING_EFFORT': 'low'},
                       'something_else': {'kept': True}}, f)
        self.mod.save_connection(model='prov/model')
        with open(self.mod.config_path(), encoding='utf-8') as f:
            data = json.load(f)
        self.assertEqual(data['env']['STRIX_REASONING_EFFORT'], 'low')
        self.assertEqual(data['something_else'], {'kept': True})

    def test_a_model_can_be_changed_without_resending_the_key(self):
        self.mod.save_connection(model='prov/one', api_key=SECRET)
        self.mod.save_connection(model='prov/two')
        env = self.mod.saved_env()
        self.assertEqual(env['STRIX_LLM'], 'prov/two')
        self.assertEqual(env['LLM_API_KEY'], SECRET)

    def test_an_empty_string_clears_a_field(self):
        self.mod.save_connection(model='prov/one', api_key=SECRET)
        self.mod.save_connection(api_key='')
        assert_env_lacks_all(self, self.mod.saved_env(), ('LLM_API_KEY',))

    def test_a_local_model_server_address_is_stored(self):
        self.mod.save_connection(model='ollama/llama',
                                 api_base='http://localhost:11434')
        self.assertEqual(self.mod.saved_env()['LLM_API_BASE'],
                         'http://localhost:11434')

    def test_control_characters_and_absurd_lengths_are_refused(self):
        for kwargs in ({'model': 'bad\nmodel'}, {'model': 'x' * 400},
                       {'api_base': 'http://x\x00'}, {'api_key': 'k\ney'}):
            view, err = self.mod.save_connection(**kwargs)
            self.assertIsNone(view, kwargs)
            self.assertIsNotNone(err)

    def test_clearing_forgets_the_model_and_key_but_nothing_else(self):
        self.mod.save_connection(model='prov/model', api_key=SECRET)
        os.makedirs(self.mod.config_dir(), exist_ok=True)
        data = self.mod.read_config()
        data['env']['STRIX_REASONING_EFFORT'] = 'high'
        self.mod._write_config(data)
        self.mod.clear_connection()
        with open(self.mod.config_path(), encoding='utf-8') as f:
            json.load(f)
        env = self.mod.saved_env()
        # Through the helpers, not assertNotIn: this mapping holds the user's
        # API key, and a bare membership assertion renders the whole thing on
        # failure (#562).
        assert_env_lacks_all(self, env, ('STRIX_LLM', 'LLM_API_KEY'))
        self.assertEqual(env.get('STRIX_REASONING_EFFORT'), 'high')

    def test_a_corrupt_config_file_is_survivable(self):
        os.makedirs(self.mod.config_dir(), exist_ok=True)
        with open(self.mod.config_path(), 'w', encoding='utf-8') as f:
            f.write('{not json')
        self.assertEqual(self.mod.read_config(), {})
        view, err = self.mod.save_connection(model='prov/model')
        self.assertIsNone(err)
        self.assertEqual(view['model'], 'prov/model')


class ViewTests(ConnectionTestCase):

    def test_the_view_never_contains_the_key(self):
        """This is what an API response is built from."""
        self.mod.save_connection(model='prov/model', api_key=SECRET)
        view = self.mod.connection_view()
        self.assertNotIn(SECRET, json.dumps(view))
        self.assertTrue(view['has_key'])

    def test_a_key_model_needs_both_to_count_as_configured(self):
        self.mod.save_connection(model='prov/model')
        self.assertFalse(self.mod.connection_view()['configured'])
        self.mod.save_connection(api_key=SECRET)
        self.assertTrue(self.mod.connection_view()['configured'])

    def test_a_subscription_model_is_configured_without_a_key(self):
        """Saying otherwise would tell a signed-in user they are not
        connected."""
        self.mod.save_connection(model='chatgpt/some-model')
        view = self.mod.connection_view()
        self.assertTrue(view['uses_subscription'])
        self.assertTrue(view['configured'])
        self.assertFalse(view['has_key'])

    def test_nothing_saved_reads_as_unconfigured(self):
        view = self.mod.connection_view()
        self.assertEqual(view['model'], '')
        self.assertFalse(view['configured'])


class InstallStateTests(ConnectionTestCase):

    def fake_install(self, ok=True, detail=''):
        class Runner:
            calls = []

            def install(self, venv_dir, package, version):
                Runner.calls.append((venv_dir, package, version))
                if ok:
                    binroot = os.path.join(venv_dir, 'bin')
                    os.makedirs(binroot, exist_ok=True)
                    open(os.path.join(binroot, 'strix'), 'w').close()
                return ok, detail
        return Runner()

    def test_nothing_installed_reads_as_absent(self):
        self.assertEqual(self.mod.install_state()['state'], 'absent')

    def test_installing_reports_ready_and_records_the_pinned_version(self):
        os.environ['STRIX_VERSION'] = '9.9.9'
        self.addCleanup(os.environ.pop, 'STRIX_VERSION', None)
        runner = self.fake_install()
        self.mod._install(runner)
        state = self.mod.install_state()
        self.assertEqual(state['state'], 'ready')
        self.assertEqual(state['version'], '9.9.9')
        self.assertEqual(runner.calls[0][2], '9.9.9')

    def test_the_deployment_decides_the_version_not_the_user(self):
        self.assertEqual(self.mod.pinned_version(), self.mod.DEFAULT_VERSION)
        os.environ['STRIX_VERSION'] = '1.2.3'
        self.addCleanup(os.environ.pop, 'STRIX_VERSION', None)
        self.assertEqual(self.mod.pinned_version(), '1.2.3')

    def test_a_failed_install_keeps_the_reason(self):
        self.mod._install(self.fake_install(ok=False, detail='no network'))
        state = self.mod.install_state()
        self.assertEqual(state['state'], 'failed')
        self.assertIn('no network', state['error'])

    def test_a_restart_during_an_install_does_not_spin_forever(self):
        """`installing` with no thread behind it is a lie the UI would show as
        a spinner that never resolves."""
        self.mod._write_install_state('installing')
        self.assertEqual(self.mod.install_state()['state'], 'failed')

    def test_ensure_installed_returns_at_once_when_already_present(self):
        self.mod._install(self.fake_install())
        self.assertEqual(self.mod.ensure_installed()['state'], 'ready')

    def test_a_second_save_during_an_install_does_not_deadlock(self):
        """`ensure_installed` used to call `install_state()` while holding the
        module lock, and `install_state()` takes that same non-reentrant lock.

        A second Save mid-install therefore blocked forever *holding* the
        lock, so every later save_connection / clear_connection / GET
        connection hung too, until the pod restarted. Run it on a worker so a
        regression fails the test instead of hanging the whole suite.
        """
        release = threading.Event()

        class Slow:
            def install(self, venv_dir, package, version):
                release.wait(10)
                return True, ''

        first = self.mod.ensure_installed(runner=Slow())
        self.addCleanup(release.set)
        self.assertEqual(first['state'], 'installing')

        done, box = threading.Event(), {}

        def second():
            try:
                box['state'] = self.mod.ensure_installed(runner=Slow())
            except Exception as e:                        # pragma: no cover
                box['error'] = e
            finally:
                done.set()

        threading.Thread(target=second, daemon=True).start()
        self.assertTrue(done.wait(5),
                        'ensure_installed deadlocked on the second call')
        self.assertIsNone(box.get('error'))
        self.assertEqual(box['state']['state'], 'installing')

        # The lock must be free afterwards, which is the half that turned one
        # stuck request into a permanently broken connection surface.
        release.set()
        self.assertIsNotNone(self.mod.install_state())
        self.assertIsNotNone(self.mod.clear_connection())


class ConnectionTestProbeTests(ConnectionTestCase):

    class Probe:
        def __init__(self, result=(True, 'ok')):
            self.result = result
            self.model_calls = []
            self.subscription_calls = []

        def subscription_status(self, executable):
            self.subscription_calls.append(executable)
            return self.result

        def probe_model(self, python, model, key, base):
            self.model_calls.append({'python': python, 'model': model,
                                     'key': key, 'base': base})
            return self.result

    def install(self):
        binroot = os.path.join(self.mod.venv_dir(), 'bin')
        os.makedirs(binroot, exist_ok=True)
        open(os.path.join(binroot, 'strix'), 'w').close()

    def test_no_test_runs_before_the_scanner_is_installed(self):
        ok, detail = self.mod.test_connection(self.Probe())
        self.assertFalse(ok)
        self.assertIn('installed', detail)

    def test_a_key_model_is_probed_with_the_exact_model_string(self):
        self.install()
        self.mod.save_connection(model='prov/model', api_key=SECRET)
        probe = self.Probe()
        ok, _ = self.mod.test_connection(probe)
        self.assertTrue(ok)
        self.assertEqual(probe.model_calls[0]['model'], 'prov/model')
        self.assertEqual(probe.model_calls[0]['key'], SECRET)

    def test_a_subscription_model_is_checked_by_asking_the_scanner(self):
        self.install()
        self.mod.save_connection(model='chatgpt/some-model')
        probe = self.Probe()
        self.mod.test_connection(probe)
        self.assertEqual(probe.model_calls, [])
        self.assertEqual(len(probe.subscription_calls), 1)

    def test_a_model_with_no_key_is_refused_without_a_network_call(self):
        self.install()
        self.mod.save_connection(model='prov/model')
        probe = self.Probe()
        ok, detail = self.mod.test_connection(probe)
        self.assertFalse(ok)
        self.assertIn('API key', detail)
        self.assertEqual(probe.model_calls, [])

    def test_no_model_at_all_is_refused(self):
        self.install()
        ok, detail = self.mod.test_connection(self.Probe())
        self.assertFalse(ok)
        self.assertIn('No model', detail)

    def test_the_probe_script_passes_no_credential_on_a_command_line(self):
        """An argv is visible to anything that can read the process table."""
        self.assertNotIn('api_key=', self.mod._PROBE.replace('"api_key"', ''))
        self.assertIn('os.environ', self.mod._PROBE)


class ProviderErrorTests(ConnectionTestCase):

    def test_a_spending_limit_is_explained(self):
        text = self.mod.explain_provider_error(
            'OpenrouterException - Key limit exceeded (total limit)')
        self.assertIn('no quota left', text)
        self.assertIn('Key limit exceeded', text)

    def test_a_wrong_model_name_is_explained(self):
        text = self.mod.explain_provider_error(
            'NotFoundError: This model is unavailable')
        self.assertIn('not available', text)

    def test_a_rejected_key_is_explained(self):
        self.assertIn('did not accept',
                      self.mod.explain_provider_error('401 Unauthorized'))

    def test_a_busy_provider_is_explained(self):
        self.assertIn('busy', self.mod.explain_provider_error(
            'GeminiException: 503 UNAVAILABLE, the model is overloaded'))

    def test_a_busy_provider_is_not_mistaken_for_a_wrong_model_name(self):
        """Both errors contain the word "unavailable". Reading a temporary
        overload as a bad model name sends the user off to change a setting
        that was correct — this ordering is why the table is ordered."""
        busy = self.mod.explain_provider_error('503 UNAVAILABLE: overloaded')
        missing = self.mod.explain_provider_error(
            'This model is unavailable for free.')
        self.assertIn('busy', busy)
        self.assertIn('not available', missing)

    def test_a_spending_limit_reported_as_403_is_not_read_as_a_bad_key(self):
        """The real shape of the error this feature first hit."""
        text = self.mod.explain_provider_error(
            'OpenrouterException - Key limit exceeded (total limit), code 403')
        self.assertIn('no quota left', text)

    def test_an_exhausted_allowance_is_not_read_as_a_busy_provider(self):
        """Verbatim from a real scan: a spent daily allowance arrives as a
        429, the same code a momentary overload uses. Telling the user to
        "wait a moment" for a cap that only resets tomorrow leaves them
        retrying a scan that cannot start."""
        text = self.mod.explain_provider_error(
            'litellm.RateLimitError: vertex_ai_betaException - {"error": '
            '{"code": 429, "message": "You exceeded your current quota, '
            'please check your plan and billing details. Quota exceeded for '
            'metric: generativelanguage.googleapis.com/'
            'generate_content_free_tier_requests, limit: 20, model: '
            'gemini-3.5-flash", "status": "RESOURCE_EXHAUSTED"}}')
        self.assertIn('no quota left', text)
        self.assertNotIn('busy', text)

    def test_a_momentary_overload_still_reads_as_busy(self):
        """The other side of that ordering: a 429 with no allowance language
        is a burst, and waiting really is the right advice."""
        text = self.mod.explain_provider_error(
            'RateLimitError: 429 Too Many Requests, please slow down')
        self.assertIn('busy', text)

    def test_the_real_transient_503_reads_as_busy(self):
        """Verbatim from a real scan, including the "UNAVAILABLE" status that
        the wrong-model rule also matches."""
        text = self.mod.explain_provider_error(
            'litellm.ServiceUnavailableError: GeminiException - {"error": '
            '{"code": 503, "message": "This model is currently experiencing '
            'high demand. Spikes in demand are usually temporary. Please try '
            'again later.", "status": "UNAVAILABLE"}}')
        self.assertIn('busy', text)

    def test_the_real_retired_model_404_reads_as_a_bad_model_name(self):
        """Verbatim from a real scan. A provider retiring a model produces a
        404, which must not be read as a key or quota problem."""
        text = self.mod.explain_provider_error(
            'litellm.NotFoundError: GeminiException - {"error": {"code": 404, '
            '"message": "This model models/gemini-2.5-flash is no longer '
            'available to new users."}}')
        self.assertIn('not available on this provider', text)

    def test_an_unrecognised_error_is_passed_through_not_swallowed(self):
        """The provider knows things we do not; hiding its message makes a
        novel failure undiagnosable."""
        text = self.mod.explain_provider_error('Something entirely new broke')
        self.assertIn('Something entirely new broke', text)

    def test_an_empty_error_still_says_something(self):
        self.assertTrue(self.mod.explain_provider_error('').strip())


class KnownSecretRedactionTests(ConnectionTestCase):
    """Exact-value redaction, which is the half that cannot be fooled.

    `scans.redact_secrets` matches credential *shapes* and is the only defence
    available to code that does not hold the key. This module does hold it, so
    these assert the stronger guarantee — including for a key whose format no
    pattern anticipates, which is the case that motivated it.
    """

    #: Synthetic, and deliberately unlike any real provider's format: a fixture
    #: that looks real trips secret scanning, and one derived from a real key
    #: is a leak. All it has to do is slip past every shape pattern.
    KEY = 'FAKE.not-a-real-credential.0123456789abcdefABCDEF'

    def test_a_key_the_shape_matcher_misses_is_still_removed(self):
        """A key in a format no pattern anticipates. `sk-…` patterns do not
        match it, so only exact-value redaction removes it."""
        key = self.KEY
        self.mod.save_connection(model='gemini/some-model', api_key=key)
        leaked = ('GeminiException: request failed for key=' + key +
                  ' — check your plan')
        cleaned = self.mod.redact_known_secrets(leaked)
        self.assertNotIn(key, cleaned)
        self.assertIn('[redacted]', cleaned)
        # The rest of the message has to survive, or the user cannot act on it.
        self.assertIn('check your plan', cleaned)

    def test_redaction_is_a_no_op_when_no_key_is_saved(self):
        self.assertEqual(
            self.mod.redact_known_secrets('nothing secret here'),
            'nothing secret here')

    def test_a_short_saved_value_never_blanks_the_message(self):
        """Redacting a 3-character "key" would replace fragments of ordinary
        words and destroy the error the user needs."""
        self.mod.save_connection(model='x/y', api_key='abc')
        text = 'abcdef: the abc provider refused the request'
        self.assertEqual(self.mod.redact_known_secrets(text), text)

    def test_empty_input_is_handled(self):
        self.mod.save_connection(model='x/y', api_key='a-long-enough-key')
        self.assertEqual(self.mod.redact_known_secrets(''), '')
        self.assertEqual(self.mod.redact_known_secrets(None), '')

    def test_a_failed_connection_test_never_echoes_the_key(self):
        """End to end: the provider quotes the request back, and what reaches
        the caller must not carry the credential."""
        key = self.KEY
        self.mod.save_connection(model='gemini/some-model', api_key=key)

        class EchoingProbe:
            def probe_model(self, python, model, probe_key, base):
                return False, 'AuthenticationError: bad key ' + probe_key

        self.mod.ensure_installed = lambda: None
        original = self.mod.is_installed
        self.mod.is_installed = lambda: True
        self.addCleanup(setattr, self.mod, 'is_installed', original)

        ok, detail = self.mod.test_connection(runner=EchoingProbe())
        self.assertFalse(ok)
        self.assertNotIn(key, detail)


class SubscriptionSignInTests(ConnectionTestCase):
    """The screen-reading half of the sign-in.

    Pure text in, state out — nothing here starts a session, so the suite
    never touches tmux or the scanner. What is worth pinning is that a
    finished command is never read as still waiting, and that the pasted
    secret is gated on shape before it is forwarded anywhere.
    """

    def setUp(self):
        super().setUp()
        self.signin = self.mod.SubscriptionSignIn

    def test_the_signin_url_is_read_off_the_screen(self):
        screen = (
            'Open this link to sign in:\n'
            '  https://auth.openai.com/oauth/authorize?client_id=x&state=y\n'
        )
        self.assertEqual(
            self.signin.parse_url(screen),
            'https://auth.openai.com/oauth/authorize?client_id=x&state=y')

    def test_a_wrapped_url_is_rejoined_before_it_is_read(self):
        # tmux capture is asked for with -J, so a wrapped URL arrives whole.
        screen = 'https://auth.openai.com/oauth/authorize?a=1&b=2&c=3'
        self.assertEqual(self.signin.parse_url(screen), screen)

    def test_an_unrelated_link_is_not_mistaken_for_the_signin(self):
        for screen in ('', 'see https://example.invalid/docs for help', None):
            self.assertIsNone(self.signin.parse_url(screen))

    def test_a_screen_with_nothing_yet_reads_as_pending(self):
        self.assertEqual(self.signin.classify('starting...')[0], 'pending')

    def test_the_prompt_means_it_is_waiting_for_the_user(self):
        state, _ = self.signin.classify(
            'Paste the full redirect URL (or code#state): ')
        self.assertEqual(state, 'awaiting_paste')

    def test_success_is_recognised(self):
        state, err = self.signin.classify(
            'Signed in with your ChatGPT subscription\n'
            '__KC_SCAN_SIGNIN_EXIT__:0')
        self.assertEqual(state, 'success')
        self.assertIsNone(err)

    def test_a_failure_carries_the_scanners_own_reason(self):
        state, err = self.signin.classify(
            'SIGN-IN FAILED: state did not match\n__KC_SCAN_SIGNIN_EXIT__:1')
        self.assertEqual(state, 'failed')
        self.assertIn('state did not match', err)

    def test_a_finished_command_is_never_read_as_still_waiting(self):
        """The prompt text stays on screen after the command exits, so the
        exit sentinel has to win — otherwise the UI waits forever on a
        sign-in that has already failed."""
        screen = ('Paste the full redirect URL (or code#state): \n'
                  'SIGN-IN FAILED\n__KC_SCAN_SIGNIN_EXIT__:1')
        self.assertEqual(self.signin.classify(screen)[0], 'failed')

    def test_a_failure_without_a_reason_still_says_something(self):
        state, err = self.signin.classify('__KC_SCAN_SIGNIN_EXIT__:1')
        self.assertEqual(state, 'failed')
        self.assertTrue(err)

    def test_a_pasted_value_of_the_wrong_shape_is_refused(self):
        for bad in ('', '   ', 'short', 'has spaces in it', 'x' * 5000):
            ok, err = self.signin.submit(bad)
            self.assertFalse(ok, bad)
            self.assertTrue(err)

    def test_a_plausible_paste_is_refused_when_nothing_is_waiting(self):
        """Shape alone is not enough — there has to be a live session to give
        it to, so a stale paste is never forwarded anywhere."""
        ok, err = self.signin.submit(
            'https://localhost:1455/auth/callback?code=abc123&state=xyz')
        self.assertFalse(ok)
        self.assertIn('Start again', err)


class SubscriptionViewTests(ConnectionTestCase):

    def test_not_signed_in_when_the_scanner_has_no_credentials(self):
        self.assertFalse(self.mod.subscription_view()['signed_in'])

    def test_signed_in_is_derived_from_the_scanners_own_file(self):
        os.makedirs(self.mod.config_dir(), exist_ok=True)
        open(self.mod.subscription_auth_path(), 'w').close()
        self.assertTrue(self.mod.subscription_view()['signed_in'])

    def test_the_connection_view_reports_it_without_reading_the_file(self):
        """Presence only. The token is the scanner's business, and nothing
        here reads, copies or reports it."""
        os.makedirs(self.mod.config_dir(), exist_ok=True)
        with open(self.mod.subscription_auth_path(), 'w') as f:
            f.write('{"access_token": "must-never-be-read"}')
        self.mod.save_connection(model='chatgpt/some-model')
        view = self.mod.connection_view()
        self.assertTrue(view['subscription']['signed_in'])
        self.assertNotIn('must-never-be-read', json.dumps(view))

    def test_a_subscription_model_is_configured_with_no_key(self):
        self.mod.save_connection(model='chatgpt/some-model')
        self.assertTrue(self.mod.connection_view()['configured'])


if __name__ == '__main__':
    unittest.main()
