"""Unit tests for the Hypervisor render tools in mcp_dashboard.py.

The render tools (show_app_preview, show_media) exist so the agent can render
live app previews / images / videos inline in the chat. The render signal is the
tool CALL (name + input) which the frontend keys off; these tests cover the
argument validation and registration.

Run with:    python3 -m unittest tests.mcp_dashboard_test
(from charts/workspace/)
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import mcp_dashboard as m  # noqa: E402


class RenderToolsTest(unittest.TestCase):
    def test_tools_registered_as_read(self):
        for name in ('show_app_preview', 'show_media', 'show_file'):
            self.assertIn(name, m.TOOLS)
            self.assertEqual(m.TOOLS[name]['kind'], 'read')  # available under READONLY_MODE

    def test_show_file_requires_a_path(self):
        self.assertTrue(m._t_show_file({}).get('isError'))
        self.assertTrue(m._t_show_file({'path': ''}).get('isError'))
        # A URL is not a workspace file — reject it (that's show_media's job).
        self.assertTrue(m._t_show_file({'path': 'https://x/a.pdf'}).get('isError'))

    def test_show_file_accepts_a_workspace_path(self):
        self.assertFalse(m._t_show_file({'path': 'docs/plan.md'}).get('isError'))
        self.assertFalse(m._t_show_file({'path': 'report.pdf', 'title': 'Q3'}).get('isError'))

    def test_show_media_requires_exactly_one_source(self):
        self.assertTrue(m._t_show_media({'media_kind': 'image'}).get('isError'))
        self.assertTrue(m._t_show_media(
            {'media_kind': 'image', 'path': 'a.png', 'url': 'http://x/a.png'}).get('isError'))

    def test_show_media_rejects_bad_kind_and_scheme(self):
        self.assertTrue(m._t_show_media({'media_kind': 'gif', 'path': 'a.gif'}).get('isError'))
        self.assertTrue(m._t_show_media(
            {'media_kind': 'image', 'url': 'ftp://x/a.png'}).get('isError'))

    def test_show_media_accepts_path_and_url(self):
        self.assertFalse(m._t_show_media({'media_kind': 'image', 'path': 'shot.png'}).get('isError'))
        self.assertFalse(m._t_show_media(
            {'media_kind': 'video', 'url': 'https://x/clip.mp4'}).get('isError'))

    def test_show_app_preview_requires_positive_port(self):
        self.assertTrue(m._t_show_app_preview({}).get('isError'))
        self.assertTrue(m._t_show_app_preview({'port': 'abc'}).get('isError'))
        self.assertTrue(m._t_show_app_preview({'port': 0}).get('isError'))
        # A valid port doesn't error even when /api/apps is unreachable in tests.
        self.assertFalse(m._t_show_app_preview({'port': 3000}).get('isError'))


if __name__ == '__main__':
    unittest.main()
