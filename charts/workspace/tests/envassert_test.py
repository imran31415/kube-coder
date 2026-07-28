"""Tests for tests/envassert.py — the no-leak assertion helpers (#562).

The whole point of the helper is what it does NOT print, so the load-bearing
test here is `test_failure_message_contains_no_values`: it builds an env
holding realistic secret material, forces a failure, and asserts none of those
values appear anywhere in the message. Without that, the helper could quietly
regress to rendering the mapping and nobody would notice until a CI log
carried a private key again.

Also guards the source files themselves: no test may go back to asserting
against a whole env mapping.

Run with:    python3 -m unittest tests.envassert_test
(from charts/workspace/)
"""

from __future__ import annotations

import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from envassert import (  # noqa: E402
    assert_env_lacks,
    assert_env_has,
    assert_env_lacks_all,
)

# Shaped like the real thing, so a substring check is meaningful.
SECRETS = {
    'GITHUB_APP_PRIVATE_KEY': '-----BEGIN RSA PRIVATE KEY-----\nMIIEogIBAAKCAQEAwD91I9DDVE\n-----END RSA PRIVATE KEY-----\n',
    'GH_TOKEN': 'ghs_2uTC5ez7n5ynKca0RgB8jVZszp4vX0467nj3',
    'OPENROUTER_API_KEY': 'sk-or-v1-b90b2f1a4351a22e12376d691378928e',
    'CONTROLLER_SELF_SERVE_TOKEN': 'f7d66a5575caad3de6da729415d619cc',
    'ANTHROPIC_API_KEY': 'sk-ant-api03-notreal',
}


class NoLeakTests(unittest.TestCase):
    def test_failure_message_contains_no_values(self):
        """The reason this module exists. Must never regress."""
        env = dict(SECRETS, KC_PROJECT_ID='kube-coder')
        with self.assertRaises(AssertionError) as ctx:
            assert_env_lacks(self, env, 'KC_PROJECT_ID')
        msg = str(ctx.exception)
        for name, value in SECRETS.items():
            self.assertNotIn(value, msg, '{} VALUE leaked into the failure'.format(name))
            # Key names are fine and useful — assert they ARE shown.
            self.assertIn(name, msg, '{} key name should be shown'.format(name))

    def test_failure_names_the_offending_key(self):
        with self.assertRaises(AssertionError) as ctx:
            assert_env_lacks(self, {'KC_EFFORT': 'high'}, 'KC_EFFORT')
        self.assertIn('KC_EFFORT', str(ctx.exception))

    def test_passes_when_absent(self):
        assert_env_lacks(self, dict(SECRETS), 'NOT_PRESENT')

    def test_lacks_all_reports_only_the_present_ones(self):
        with self.assertRaises(AssertionError) as ctx:
            assert_env_lacks_all(self, dict(SECRETS), ['GH_TOKEN', 'ABSENT_ONE'])
        msg = str(ctx.exception)
        self.assertIn('GH_TOKEN', msg)
        self.assertNotIn(SECRETS['GH_TOKEN'], msg)
        self.assertNotIn('ABSENT_ONE', msg.split('env keys:')[0])

    def test_env_has_does_not_dump_on_missing(self):
        with self.assertRaises(AssertionError) as ctx:
            assert_env_has(self, dict(SECRETS), 'MISSING')
        for value in SECRETS.values():
            self.assertNotIn(value, str(ctx.exception))

    def test_key_list_is_truncated_on_a_huge_env(self):
        big = {'VAR_{:03d}'.format(i): 'v' for i in range(200)}
        with self.assertRaises(AssertionError) as ctx:
            assert_env_lacks(self, dict(big, TARGET='x'), 'TARGET')
        self.assertIn('more)', str(ctx.exception))


class NoWholeEnvAssertionsRemain(unittest.TestCase):
    """Stop the pattern coming back."""

    # assertNotIn/assertIn against something that looks like an env mapping.
    PATTERN = re.compile(r"assert(?:Not)?In\(\s*['\"][A-Z0-9_]+['\"]\s*,\s*[\w\[\]'\"]*env[\w\[\]'\"]*\s*\)")

    def test_no_test_asserts_against_a_whole_env_mapping(self):
        offenders = []
        for name in sorted(os.listdir(HERE)):
            if not name.endswith('_test.py'):
                continue
            path = os.path.join(HERE, name)
            with open(path, 'r', encoding='utf-8') as fh:
                for i, line in enumerate(fh, 1):
                    if self.PATTERN.search(line):
                        offenders.append('{}:{}  {}'.format(name, i, line.strip()))
        self.assertEqual(offenders, [], (
            '\n\nThese assert membership against a whole environment mapping, so '
            'unittest renders every value — including secrets — when they fail '
            '(#562). Use assert_env_lacks/assert_env_has from tests/envassert.py:'
            '\n\n  ' + '\n  '.join(offenders) + '\n'))


if __name__ == '__main__':
    unittest.main()
