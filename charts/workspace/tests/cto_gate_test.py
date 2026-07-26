"""Tests for the AI CTO config gate + Mission Control integration (#467).

Covers cto_available() (rides the Hypervisor), the _require_cto() 404 gate on
the projects API, the persona downgrade when the feature is off, and the
persona/project_id fields on a Mission Control chat card.

Run:  python3 -m unittest tests.cto_gate_test
"""

import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402


class _Flags:
    """Context manager to set the three gate globals and restore them."""

    def __init__(self, cto, hv, avail):
        self.vals = {'CTO_ENABLED': cto, 'HYPERVISOR_ENABLED': hv,
                     '_HYPERVISOR_AVAILABLE': avail}

    def __enter__(self):
        self.orig = {k: getattr(server, k) for k in self.vals}
        for k, v in self.vals.items():
            setattr(server, k, v)
        return self

    def __exit__(self, *a):
        for k, v in self.orig.items():
            setattr(server, k, v)


class CtoAvailableTests(unittest.TestCase):
    def test_requires_flag_and_hypervisor(self):
        with _Flags(True, True, True):
            self.assertTrue(server.cto_available())
        # Any one off → unavailable (it rides the Hypervisor).
        with _Flags(False, True, True):
            self.assertFalse(server.cto_available())
        with _Flags(True, False, True):
            self.assertFalse(server.cto_available())
        with _Flags(True, True, False):
            self.assertFalse(server.cto_available())


class RequireCtoGateTests(unittest.TestCase):
    def _handler(self):
        h = mock.Mock(spec=server.BrowserHandler)
        self.responses = []
        h.send_json.side_effect = \
            lambda o, s=200: self.responses.append((o, s))
        return h

    def test_gate_passes_when_available(self):
        h = self._handler()
        with mock.patch.object(server, 'cto_available', return_value=True):
            self.assertTrue(server.BrowserHandler._require_cto(h))
        self.assertEqual(self.responses, [])

    def test_gate_404s_when_disabled(self):
        h = self._handler()
        with mock.patch.object(server, 'cto_available', return_value=False):
            self.assertFalse(server.BrowserHandler._require_cto(h))
        self.assertEqual(self.responses[-1][1], 404)


class ProjectApiGateTests(unittest.TestCase):
    def _handler(self):
        h = mock.Mock(spec=server.BrowserHandler)
        h.check_claude_auth.return_value = True
        # Route the gate helper through the real implementation.
        h._require_cto.side_effect = \
            lambda: server.BrowserHandler._require_cto(h)
        self.responses = []
        h.send_json.side_effect = \
            lambda o, s=200: self.responses.append((o, s))
        return h

    def test_list_projects_404s_when_cto_disabled(self):
        h = self._handler()
        with mock.patch.object(server, 'cto_available', return_value=False), \
             mock.patch.object(server.ProjectsManager, 'list_projects') as lp:
            server.BrowserHandler.handle_project_list(h)
        self.assertEqual(self.responses[-1][1], 404)
        lp.assert_not_called()  # gated before any work

    def test_list_projects_ok_when_enabled(self):
        h = self._handler()
        with mock.patch.object(server, 'cto_available', return_value=True), \
             mock.patch.object(server.ProjectsManager, 'list_projects',
                               return_value=[]):
            server.BrowserHandler.handle_project_list(h)
        self.assertEqual(self.responses[-1], ({'projects': []}, 200))


class MissionCardPersonaTests(unittest.TestCase):
    def test_thread_card_carries_persona_and_project(self):
        summary = {
            'id': 't1', 'title': 'Weekly review', 'status': 'running',
            'assistant': 'claude', 'model': '', 'created_at': 100.0,
            'updated_at': 200.0, 'persona': 'cto', 'project_id': 'kube-coder',
        }
        with mock.patch.object(server, '_HYPERVISOR_AVAILABLE', False):
            # thread_dir headline path is skipped when unavailable; fine here.
            card = server._mc_thread_card(summary, now=250.0)
        self.assertIsNotNone(card)
        self.assertEqual(card['kind'], 'chat')
        self.assertEqual(card['persona'], 'cto')
        self.assertEqual(card['project_id'], 'kube-coder')

    def test_plain_thread_card_has_empty_persona(self):
        summary = {
            'id': 't2', 'title': 'chat', 'status': 'running',
            'created_at': 100.0, 'updated_at': 200.0,
        }
        with mock.patch.object(server, '_HYPERVISOR_AVAILABLE', False):
            card = server._mc_thread_card(summary, now=250.0)
        self.assertEqual(card['persona'], '')
        self.assertEqual(card['project_id'], '')


if __name__ == '__main__':
    unittest.main()
