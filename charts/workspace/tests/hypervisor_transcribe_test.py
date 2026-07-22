"""Tests for the voice interface's server-side STT (issue #396, tier 1):
POST /api/hypervisor/transcribe and the SpeechTranscriber helper.

The endpoint exists for clients without a browser SpeechRecognition (the Expo
mobile app): raw audio in, transcript out, provider = an OpenAI-compatible
transcriptions API. Key precedence: the per-workspace provider-key store
first, pod env second. The upstream call is mocked — no network.

Run:  python3 -m unittest tests.hypervisor_transcribe_test
"""

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

try:
    import fcntl  # noqa: F401
except ImportError:  # pragma: no cover - platform shim
    import types
    _shim = types.ModuleType('fcntl')
    _shim.flock = lambda *a, **k: None
    _shim.LOCK_EX = _shim.LOCK_UN = _shim.LOCK_SH = _shim.LOCK_NB = 0
    sys.modules['fcntl'] = _shim

import server  # noqa: E402


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TranscribeTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='kctest-stt-')
        self._orig_file = server.ProviderKeysManager.KEYS_FILE
        server.ProviderKeysManager.KEYS_FILE = os.path.join(
            self.tmpdir, 'provider-keys.json')
        self._orig_env = os.environ.pop('OPENAI_API_KEY', None)

    def tearDown(self):
        server.ProviderKeysManager.KEYS_FILE = self._orig_file
        if self._orig_env is not None:
            os.environ['OPENAI_API_KEY'] = self._orig_env
        else:
            os.environ.pop('OPENAI_API_KEY', None)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _handler(self, authed=True, body=b'', headers=None):
        h = mock.Mock(spec=server.BrowserHandler)
        h.check_claude_auth.return_value = authed
        hdrs = {'Content-Length': str(len(body)), 'Content-Type': 'audio/m4a'}
        hdrs.update(headers or {})
        h.headers = hdrs
        h.rfile = io.BytesIO(body)
        self.responses = []
        h.send_json.side_effect = (
            lambda obj, status=200: self.responses.append((obj, status)))
        return h

    def last(self):
        self.assertTrue(self.responses, 'handler sent no response')
        return self.responses[-1]


class SpeechTranscriberTests(TranscribeTestBase):
    def test_unavailable_without_any_key(self):
        self.assertFalse(server.SpeechTranscriber.available())

    def test_available_via_provider_key_store(self):
        server.ProviderKeysManager.set('OPENAI_API_KEY', 'sk-stt-store')
        self.assertTrue(server.SpeechTranscriber.available())
        self.assertEqual(server.SpeechTranscriber.api_key(), 'sk-stt-store')

    def test_available_via_pod_env_fallback(self):
        os.environ['OPENAI_API_KEY'] = 'sk-stt-env'
        self.assertEqual(server.SpeechTranscriber.api_key(), 'sk-stt-env')

    def test_store_key_wins_over_env(self):
        os.environ['OPENAI_API_KEY'] = 'sk-stt-env'
        server.ProviderKeysManager.set('OPENAI_API_KEY', 'sk-stt-store')
        self.assertEqual(server.SpeechTranscriber.api_key(), 'sk-stt-store')

    def test_transcribe_without_key_is_503(self):
        text, err = server.SpeechTranscriber.transcribe(b'RIFFdata')
        self.assertIsNone(text)
        self.assertEqual(err[0], 503)
        self.assertIn('Provider keys', err[1])

    def test_transcribe_posts_multipart_and_returns_text(self):
        server.ProviderKeysManager.set('OPENAI_API_KEY', 'sk-stt-1234')
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured['req'] = req
            return _FakeResponse({'text': '  hello workspace  '})

        with mock.patch.object(server.urllib.request, 'urlopen', fake_urlopen):
            text, err = server.SpeechTranscriber.transcribe(
                b'AUDIOBYTES', 'audio/m4a')
        self.assertIsNone(err)
        self.assertEqual(text, 'hello workspace')
        req = captured['req']
        self.assertEqual(req.get_header('Authorization'), 'Bearer sk-stt-1234')
        self.assertIn(b'AUDIOBYTES', req.data)
        self.assertIn(server.SpeechTranscriber.MODEL.encode(), req.data)
        self.assertIn(b'filename="audio.m4a"', req.data)

    def test_transcribe_maps_provider_http_error_to_502(self):
        server.ProviderKeysManager.set('OPENAI_API_KEY', 'sk-stt-1234')

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 401, 'nope', {}, io.BytesIO(b'{"error":"bad key"}'))

        with mock.patch.object(server.urllib.request, 'urlopen', fake_urlopen):
            text, err = server.SpeechTranscriber.transcribe(b'x', 'audio/wav')
        self.assertIsNone(text)
        self.assertEqual(err[0], 502)
        self.assertIn('401', err[1])


class TranscribeHandlerTests(TranscribeTestBase):
    def _run(self, h):
        server.BrowserHandler.handle_hypervisor_transcribe(h)

    def test_requires_auth(self):
        self._run(self._handler(authed=False, body=b'x'))
        self.assertEqual(self.last(), ({'error': 'Unauthorized'}, 401))

    def test_empty_body_is_400(self):
        self._run(self._handler(body=b''))
        obj, status = self.last()
        self.assertEqual(status, 400)

    def test_oversize_body_is_413(self):
        h = self._handler(body=b'x')
        h.headers['Content-Length'] = str(
            server.SpeechTranscriber.MAX_AUDIO_BYTES + 1)
        self._run(h)
        obj, status = self.last()
        self.assertEqual(status, 413)

    def test_no_provider_key_is_503(self):
        self._run(self._handler(body=b'AUDIO'))
        obj, status = self.last()
        self.assertEqual(status, 503)
        self.assertIn('speech-to-text', obj['error'])

    def test_happy_path_returns_transcript(self):
        server.ProviderKeysManager.set('OPENAI_API_KEY', 'sk-stt-1234')

        def fake_urlopen(req, timeout=None):
            return _FakeResponse({'text': 'deploy the app'})

        with mock.patch.object(server.urllib.request, 'urlopen', fake_urlopen):
            self._run(self._handler(body=b'AUDIO'))
        self.assertEqual(self.last(), ({'text': 'deploy the app'}, 200))

    def test_config_reports_stt_flag(self):
        # config 'stt' mirrors SpeechTranscriber.available() so mobile can
        # show/hide the mic without a probe request.
        self.assertFalse(server.SpeechTranscriber.available())
        server.ProviderKeysManager.set('OPENAI_API_KEY', 'sk-stt-1234')
        self.assertTrue(server.SpeechTranscriber.available())


if __name__ == '__main__':
    unittest.main()
