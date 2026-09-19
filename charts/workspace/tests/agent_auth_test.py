"""Provider error guidance applies to every runtime without guessing logins."""
import unittest
import io
import threading
from types import SimpleNamespace
from hypervisor_session import annotate_auth_error, auth_failure_events, is_auth_failure
from hypervisor_session import HypervisorSession
from runtimes import RUNTIMES


class AgentAuthTest(unittest.TestCase):
    def test_ante_provider_fallback_is_setup_not_authentication(self):
        unavailable = threading.Event()
        auth = threading.Event()
        session = SimpleNamespace(append_runner_log=lambda line: None)
        process = SimpleNamespace(stderr=io.StringIO("Provider 'openrouter' is unavailable; using 'local' instead\n"))
        HypervisorSession._drain_stderr(session, process, auth, unavailable)
        self.assertTrue(unavailable.is_set())
        self.assertFalse(auth.is_set())
    def test_explicit_auth_failures(self):
        for message in (
            '401 Unauthorized: Missing bearer or basic authentication in header',
            'Authentication Fails', 'invalid_api_key', 'Please log in',
            'Not signed in', 'API key is missing', 'No API key provided',
            'OAuth token expired', 'AuthenticationError', 'Unauthorized',
            'Missing Authentication header',
        ):
            with self.subTest(message=message):
                self.assertTrue(is_auth_failure(message))
                self.assertTrue(annotate_auth_error({'type': 'error', 'text': message})['auth_required'])

    def test_non_auth_failures_and_tool_errors_are_unchanged(self):
        for message in ('codex exited with code 2', '429 rate limit', '403 forbidden',
                        'Connection timed out', 'unknown option -C', 'not enough credits'):
            self.assertFalse(is_auth_failure(message), message)
        event = {'type': 'tool_result', 'text': '401 Unauthorized', 'is_error': True}
        self.assertIs(annotate_auth_error(event), event)

    def test_every_runtime_has_setup_guidance(self):
        for runtime in RUNTIMES.values():
            self.assertTrue(runtime['auth_help'].strip())

    def test_stderr_guidance_preserves_diagnostics_and_other_events(self):
        events = [{'type': 'message', 'text': 'partial response'},
                  {'type': 'error', 'text': 'codex exited with code 1'}]
        result = auth_failure_events(events, 1, True)
        self.assertEqual(result[0], events[0])
        self.assertEqual(result[1]['text'], events[1]['text'])
        self.assertTrue(result[1]['auth_required'])
        self.assertNotIn('auth_required', events[1])
        self.assertIs(auth_failure_events(events, 0, True), events)
        self.assertIs(auth_failure_events(events, 1, False), events)
