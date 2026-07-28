"""Unit tests for kc-harness reasoning-effort forwarding (#362).

kc-harness talks an OpenAI-compatible endpoint, so the hypervisor passes the
per-thread effort as KC_EFFORT and harness.py forwards it as the request's
`reasoning_effort` — omitted entirely when unset so the model keeps its default.

Run with:   python3 -m unittest tests.harness_effort_test   (from charts/workspace/)
"""

import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import harness  # noqa: E402


class PickEffortTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop('KC_EFFORT', None)

    def test_reads_and_normalizes_env(self):
        os.environ['KC_EFFORT'] = ' High '
        self.assertEqual(harness.pick_effort(), 'high')

    def test_blank_when_unset(self):
        os.environ.pop('KC_EFFORT', None)
        self.assertEqual(harness.pick_effort(), '')


class ChatBodyTests(unittest.TestCase):
    """Capture the request body chat() builds without hitting the network."""

    def tearDown(self):
        os.environ.pop('KC_EFFORT', None)

    def _capture_body(self):
        captured = {}

        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"choices":[]}'

        def fake_urlopen(req, timeout=None):
            captured['body'] = json.loads(req.data.decode())
            return _Resp()

        with mock.patch('urllib.request.urlopen', fake_urlopen):
            harness.chat([{'role': 'user', 'content': 'hi'}],
                         'http://x/v1', '', 'qwen')
        return captured['body']

    def test_includes_reasoning_effort_when_set(self):
        os.environ['KC_EFFORT'] = 'high'
        body = self._capture_body()
        self.assertEqual(body.get('reasoning_effort'), 'high')

    def test_omits_reasoning_effort_when_unset(self):
        os.environ.pop('KC_EFFORT', None)
        body = self._capture_body()
        self.assertNotIn('reasoning_effort', body)


if __name__ == '__main__':
    unittest.main()
