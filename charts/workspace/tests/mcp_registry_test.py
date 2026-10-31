"""Unit tests for mcp_registry.py (issue #353) — the user-defined MCP server
registry and its fan-out to Claude / OpenCode / Ante / Codex configs, plus
remote (http/sse) entries and the both-homes Claude write (#815).

Run with:    python3 -m unittest tests.mcp_registry_test
(from charts/workspace/)
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import mcp_registry as mr  # noqa: E402


class RegistryTestCase(unittest.TestCase):
    """All paths rebound to a temp dir so no real config is ever touched."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='mcp-registry-test-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patches = {
            'REGISTRY_FILE': os.path.join(self.tmp, 'store', 'mcp-servers.json'),
            # Both homes (#815): the server's own and /home/dev's.
            'CLAUDE_CONFIGS': (os.path.join(self.tmp, '.claude.json'),
                               os.path.join(self.tmp, 'dev', '.claude.json')),
            'OPENCODE_CONFIG': os.path.join(self.tmp, 'opencode', 'opencode.json'),
            'ANTE_SETTINGS': os.path.join(self.tmp, '.ante', 'settings.json'),
            'CODEX_HOME': os.path.join(self.tmp, '.codex'),
        }
        for attr, value in patches.items():
            p = mock.patch.object(mr, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.claude_cfg, self.claude_dev_cfg = patches['CLAUDE_CONFIGS']
        # Codex is exercised explicitly in CodexSyncTests; elsewhere the CLI
        # is "absent" so sync_all records an error without spawning anything.
        p = mock.patch.object(mr.shutil, 'which', return_value=None)
        p.start()
        self.addCleanup(p.stop)

    # helpers
    def read(self, path):
        with open(path) as f:
            return json.load(f)

    def write(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            json.dump(data, f)


class SetServerTests(RegistryTestCase):
    def test_roundtrip(self):
        ok, err = mr.set_server('github', 'npx',
                                args=['-y', '@modelcontextprotocol/server-github'],
                                env={'GITHUB_TOKEN': 'ghp_secret1234'})
        self.assertTrue(ok, err)
        view = mr.public_view()
        self.assertEqual(len(view), 1)
        self.assertEqual(view[0]['name'], 'github')
        self.assertEqual(view[0]['command'], 'npx')
        self.assertTrue(view[0]['enabled'])

    def test_rejects_bad_names(self):
        for bad in ('', 'has space', 'a/b', 'x' * 65, '../etc'):
            ok, err = mr.set_server(bad, 'cmd')
            self.assertFalse(ok, f'{bad!r} should be rejected')

    def test_rejects_reserved_names(self):
        for name in ('memory', 'dashboard', 'agent-orchestrator', 'playwright'):
            ok, err = mr.set_server(name, 'python3')
            self.assertFalse(ok)
            self.assertIn('built-in', err)

    def test_rejects_bad_shapes(self):
        self.assertFalse(mr.set_server('x', '')[0])
        self.assertFalse(mr.set_server('x', 'cmd', args='notalist')[0])
        self.assertFalse(mr.set_server('x', 'cmd', args=[1])[0])
        self.assertFalse(mr.set_server('x', 'cmd', env={'1bad': 'v'})[0])
        self.assertFalse(mr.set_server('x', 'cmd', env={'K': 7})[0])

    def test_blank_env_value_keeps_previous_secret(self):
        mr.set_server('svc', 'run', env={'TOKEN': 'tok_original99'})
        # Update posts the redacted form back with a blank TOKEN.
        ok, _ = mr.set_server('svc', 'run', env={'TOKEN': ''})
        self.assertTrue(ok)
        reg = mr._read_registry()
        self.assertEqual(reg['servers']['svc']['env']['TOKEN'], 'tok_original99')
        # A new non-empty value replaces it.
        mr.set_server('svc', 'run', env={'TOKEN': 'tok_new'})
        reg = mr._read_registry()
        self.assertEqual(reg['servers']['svc']['env']['TOKEN'], 'tok_new')

    def test_registry_file_is_0600(self):
        mr.set_server('svc', 'run', env={'TOKEN': 'secret'})
        mode = os.stat(mr.REGISTRY_FILE).st_mode & 0o777
        self.assertEqual(mode, 0o600)


class PublicViewTests(RegistryTestCase):
    def test_env_values_are_redacted(self):
        mr.set_server('svc', 'run', env={'TOKEN': 'tok_abcd1234', 'K': 'ab'})
        view = mr.public_view()
        env = view[0]['env']
        self.assertEqual(env['TOKEN'], '…1234')
        self.assertEqual(env['K'], '•••')
        self.assertNotIn('tok_abcd1234', json.dumps(view))


class FanOutTests(RegistryTestCase):
    def test_sync_writes_all_json_providers(self):
        mr.set_server('github', 'npx', args=['-y', 'server-github'],
                      env={'GITHUB_TOKEN': 't'})
        results = mr.sync_all()
        self.assertEqual(results['claude'], 'ok')
        self.assertEqual(results['opencode'], 'ok')
        self.assertEqual(results['ante'], 'ok')
        self.assertIn('skipped', results['codex'])  # CLI absent in this case

        claude = self.read(self.claude_cfg)['mcpServers']['github']
        self.assertEqual(claude, {'type': 'stdio', 'command': 'npx',
                                  'args': ['-y', 'server-github'],
                                  'env': {'GITHUB_TOKEN': 't'}})
        oc = self.read(mr.OPENCODE_CONFIG)['mcp']['github']
        self.assertEqual(oc, {'type': 'local',
                              'command': ['npx', '-y', 'server-github'],
                              'enabled': True,
                              'environment': {'GITHUB_TOKEN': 't'}})
        ante = self.read(mr.ANTE_SETTINGS)['mcp_servers']['github']
        self.assertEqual(ante, {'command': 'npx', 'args': ['-y', 'server-github'],
                                'env': {'GITHUB_TOKEN': 't'}})

    def test_sync_preserves_seeded_and_user_entries(self):
        # Simulate the boot-seeded defaults + a hand-edit in each config.
        seeded = {'type': 'stdio', 'command': 'python3', 'args': ['/x/mcp_memory.py']}
        self.write(self.claude_cfg, {'mcpServers': {'memory': seeded,
                                                     'hand-edit': seeded},
                                      'otherKey': 1})
        self.write(mr.OPENCODE_CONFIG, {'$schema': 's', 'provider': {'p': {}},
                                        'mcp': {'memory': {'type': 'local'}}})
        self.write(mr.ANTE_SETTINGS, {'mcp_servers': {'memory': {'command': 'p'}},
                                      'has_completed_onboarding': True})
        mr.set_server('mine', 'run')
        mr.sync_all()
        mr.delete_server('mine')
        mr.sync_all()

        claude = self.read(self.claude_cfg)
        self.assertEqual(claude['mcpServers']['memory'], seeded)
        self.assertEqual(claude['mcpServers']['hand-edit'], seeded)
        self.assertEqual(claude['otherKey'], 1)
        self.assertNotIn('mine', claude['mcpServers'])
        oc = self.read(mr.OPENCODE_CONFIG)
        self.assertIn('memory', oc['mcp'])
        self.assertIn('provider', oc)
        self.assertNotIn('mine', oc['mcp'])
        ante = self.read(mr.ANTE_SETTINGS)
        self.assertIn('memory', ante['mcp_servers'])
        self.assertTrue(ante['has_completed_onboarding'])
        self.assertNotIn('mine', ante['mcp_servers'])

    def test_disabled_entry_is_removed_from_providers_but_kept_in_registry(self):
        mr.set_server('svc', 'run')
        mr.sync_all()
        self.assertIn('svc', self.read(self.claude_cfg)['mcpServers'])
        mr.set_server('svc', 'run', enabled=False)
        mr.sync_all()
        self.assertNotIn('svc', self.read(self.claude_cfg)['mcpServers'])
        self.assertNotIn('svc', self.read(mr.OPENCODE_CONFIG)['mcp'])
        self.assertEqual(len(mr.public_view()), 1)
        self.assertFalse(mr.public_view()[0]['enabled'])

    def test_one_broken_provider_does_not_block_others(self):
        os.makedirs(os.path.dirname(self.claude_cfg), exist_ok=True)
        with open(self.claude_cfg, 'w') as f:
            f.write('{corrupt json')
        mr.set_server('svc', 'run')
        results = mr.sync_all()
        self.assertIn('error', results['claude'])
        self.assertEqual(results['opencode'], 'ok')
        self.assertEqual(results['ante'], 'ok')
        # The corrupt file was left alone, not clobbered.
        with open(self.claude_cfg) as f:
            self.assertEqual(f.read(), '{corrupt json')

    def test_opencode_fresh_file_gets_schema(self):
        mr.set_server('svc', 'run')
        mr.sync_all()
        oc = self.read(mr.OPENCODE_CONFIG)
        self.assertEqual(oc['$schema'], 'https://opencode.ai/config.json')

    def test_reserved_names_in_tampered_registry_are_never_fanned_out(self):
        # Even if the on-disk registry is edited by hand to include a reserved
        # name, sync must not upsert or remove it in provider configs.
        self.write(mr.REGISTRY_FILE, {
            'servers': {'memory': {'command': 'evil', 'args': [], 'env': {},
                                   'enabled': True}},
            'managed': ['memory'],
        })
        seeded = {'type': 'stdio', 'command': 'python3', 'args': ['/x.py']}
        self.write(self.claude_cfg, {'mcpServers': {'memory': seeded}})
        mr.sync_all()
        self.assertEqual(self.read(self.claude_cfg)['mcpServers']['memory'],
                         seeded)


class ClaudeBothHomesTests(RegistryTestCase):
    """#815: a Claude CLI reads $HOME/.claude.json and $HOME differs by spawn
    path, so the Claude sync writes every home's file."""

    def test_sync_writes_every_home(self):
        mr.set_server('svc', 'run', args=['a'])
        self.assertEqual(mr.sync_all()['claude'], 'ok')
        want = {'type': 'stdio', 'command': 'run', 'args': ['a']}
        for path in (self.claude_cfg, self.claude_dev_cfg):
            with self.subTest(path=path):
                self.assertEqual(self.read(path)['mcpServers']['svc'], want)

    def test_removal_reaches_every_home(self):
        mr.set_server('svc', 'run')
        mr.sync_all()
        mr.delete_server('svc')
        mr.sync_all()
        for path in (self.claude_cfg, self.claude_dev_cfg):
            with self.subTest(path=path):
                self.assertNotIn('svc', self.read(path)['mcpServers'])

    def test_one_corrupt_home_does_not_block_the_other(self):
        os.makedirs(os.path.dirname(self.claude_dev_cfg), exist_ok=True)
        with open(self.claude_dev_cfg, 'w') as f:
            f.write('{corrupt')
        mr.set_server('svc', 'run')
        results = mr.sync_all()
        self.assertIn('error', results['claude'])
        self.assertIn(self.claude_dev_cfg, results['claude'])
        self.assertIn('svc', self.read(self.claude_cfg)['mcpServers'])
        with open(self.claude_dev_cfg) as f:
            self.assertEqual(f.read(), '{corrupt')
        # The failed home keeps the name managed, so a later pass retries.
        self.assertIn('svc', mr._read_registry()['managed'])

    def test_same_file_through_a_symlink_is_written_once(self):
        self.write(self.claude_cfg, {'mcpServers': {}})
        os.makedirs(os.path.dirname(self.claude_dev_cfg), exist_ok=True)
        os.symlink(self.claude_cfg, self.claude_dev_cfg)
        self.assertEqual(mr._claude_config_paths(), [self.claude_cfg])
        mr.set_server('svc', 'run')
        self.assertEqual(mr.sync_all()['claude'], 'ok')
        self.assertTrue(os.path.islink(self.claude_dev_cfg))
        self.assertIn('svc', self.read(self.claude_dev_cfg)['mcpServers'])


class RemoteServerTests(RegistryTestCase):
    """#815: http/sse servers (e.g. Canva's hosted connector)."""
    CANVA = 'https://mcp.canva.com/mcp'

    def test_http_roundtrip_and_view(self):
        ok, err = mr.set_server('canva', transport='http', url=self.CANVA)
        self.assertTrue(ok, err)
        self.assertEqual(mr._read_registry()['servers']['canva'],
                         {'type': 'http', 'url': self.CANVA, 'headers': {},
                          'enabled': True})
        view = mr.public_view()[0]
        self.assertEqual(view['type'], 'http')
        self.assertEqual(view['url'], self.CANVA)
        self.assertEqual(view['providers'], ['claude'])
        # stdio fields stay present (empty) for clients that predate #815.
        self.assertEqual((view['command'], view['args'], view['env']),
                         ('', [], {}))

    def test_url_validation(self):
        good = ('https://mcp.canva.com/mcp', 'http://localhost:8080/mcp',
                'http://127.0.0.1/mcp', 'http://[::1]:9000/sse')
        for url in good:
            with self.subTest(url=url):
                ok, err = mr.set_server('r', transport='http', url=url)
                self.assertTrue(ok, err)
        bad = ('', None, 'mcp.canva.com/mcp', 'ftp://x.example/mcp',
               'http://example.com/mcp', 'https://user:pw@example.com/mcp',
               'https://example.com/a b', 'https://', 'https://x/\na',
               'https://' + 'a' * 2100)
        for url in bad:  # a new name: a blank url on 'r' keeps the stored one
            with self.subTest(url=url):
                self.assertFalse(mr.set_server('new', transport='http', url=url)[0])

    def test_rejects_unknown_type_and_bad_headers(self):
        self.assertFalse(mr.set_server('r', transport='ws', url=self.CANVA)[0])
        for headers in ({'bad header': 'v'}, {'X-K': 7}, {'X-K': 'a\r\nX: y'},
                        'notadict'):
            with self.subTest(headers=headers):
                self.assertFalse(mr.set_server('r', transport='http',
                                               url=self.CANVA,
                                               headers=headers)[0])

    def test_reserved_names_apply_to_remote_too(self):
        ok, err = mr.set_server('memory', transport='http', url=self.CANVA)
        self.assertFalse(ok)
        self.assertIn('built-in', err)

    def test_headers_are_redacted_and_blank_keeps_secret(self):
        mr.set_server('r', transport='http', url=self.CANVA,
                      headers={'Authorization': 'Bearer tok_abcd9876'})
        view = mr.public_view()[0]
        self.assertEqual(view['headers'], {'Authorization': '…9876'})
        self.assertNotIn('tok_abcd9876', json.dumps(mr.public_view()))
        mr.set_server('r', transport='http', url=self.CANVA,
                      headers={'Authorization': ''})
        self.assertEqual(mr._read_registry()['servers']['r']['headers'],
                         {'Authorization': 'Bearer tok_abcd9876'})

    def test_typeless_toggle_from_an_older_client_keeps_the_remote_entry(self):
        # A pre-#815 client (the shipped mobile app) toggles with the stdio
        # shape: no type, no url, no headers, blank command.
        mr.set_server('r', transport='sse', url=self.CANVA,
                      headers={'X-Key': 'secret'})
        ok, err = mr.set_server('r', '', args=[], env={}, enabled=False)
        self.assertTrue(ok, err)
        self.assertEqual(mr._read_registry()['servers']['r'],
                         {'type': 'sse', 'url': self.CANVA,
                          'headers': {'X-Key': 'secret'}, 'enabled': False})

    def test_new_typeless_entry_is_still_stdio(self):
        self.assertEqual(mr.set_server('r', '')[1], 'command is required')

    def test_legacy_untyped_registry_entry_syncs_as_stdio(self):
        self.write(mr.REGISTRY_FILE, {'servers': {'old': {
            'command': 'run', 'args': [], 'env': {}, 'enabled': True}},
            'managed': []})
        mr.sync_all()
        self.assertEqual(self.read(self.claude_cfg)['mcpServers']['old'],
                         {'type': 'stdio', 'command': 'run', 'args': []})
        self.assertEqual(mr.public_view()[0]['type'], 'stdio')

    def test_remote_sync_is_claude_only(self):
        mr.set_server('canva', transport='http', url=self.CANVA)
        mr.set_server('stream', transport='sse', url='https://x.example/sse',
                      headers={'Authorization': 'Bearer t'})
        results = mr.sync_all()
        self.assertEqual(results['claude'], 'ok')
        for path in (self.claude_cfg, self.claude_dev_cfg):
            with self.subTest(path=path):
                servers = self.read(path)['mcpServers']
                self.assertEqual(servers['canva'],
                                 {'type': 'http', 'url': self.CANVA})
                self.assertEqual(servers['stream'],
                                 {'type': 'sse', 'url': 'https://x.example/sse',
                                  'headers': {'Authorization': 'Bearer t'}})
        # Nothing stdio to write, so the stdio-only configs are not created.
        self.assertFalse(os.path.exists(mr.OPENCODE_CONFIG))
        self.assertFalse(os.path.exists(mr.ANTE_SETTINGS))

    def test_switching_stdio_to_http_leaves_stdio_only_providers(self):
        mr.set_server('svc', 'run')
        mr.sync_all()
        self.assertIn('svc', self.read(mr.OPENCODE_CONFIG)['mcp'])
        mr.set_server('svc', transport='http', url=self.CANVA)
        mr.sync_all()
        self.assertNotIn('svc', self.read(mr.OPENCODE_CONFIG)['mcp'])
        self.assertNotIn('svc', self.read(mr.ANTE_SETTINGS)['mcp_servers'])
        self.assertEqual(self.read(self.claude_cfg)['mcpServers']['svc'],
                         {'type': 'http', 'url': self.CANVA})
        self.assertIn('svc', mr._read_registry()['managed'])

    def test_codex_never_gets_a_remote_add(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return mock.Mock(returncode=0)

        mr.set_server('canva', transport='http', url=self.CANVA)
        with mock.patch.object(mr.shutil, 'which',
                               side_effect=lambda n: '/usr/bin/codex'
                               if n == 'codex' else None), \
             mock.patch.object(mr.subprocess, 'run', side_effect=fake_run):
            self.assertEqual(mr.sync_all()['codex'], 'ok')
        self.assertFalse([c for c in calls if c[:3] == ['codex', 'mcp', 'add']])


class SetHandlerTests(RegistryTestCase):
    """POST /api/mcp-servers passes the remote fields through (#815)."""

    def _post(self, body):
        import server  # heavy; only this class needs it
        h = mock.Mock(spec=server.BrowserHandler)
        h._mcp_registry_gate.return_value = True
        h.read_json_body.return_value = body
        responses = []
        h.send_json.side_effect = lambda o, s=200: responses.append((o, s))
        server.BrowserHandler.handle_mcp_servers_set(h)
        return responses[-1]

    def test_http_entry_reaches_the_claude_config(self):
        body, status = self._post({'name': 'canva', 'type': 'http',
                                   'url': 'https://mcp.canva.com/mcp',
                                   'headers': {}})
        self.assertEqual(status, 200, body)
        self.assertEqual(body['sync']['claude'], 'ok')
        self.assertEqual(self.read(self.claude_cfg)['mcpServers']['canva'],
                         {'type': 'http', 'url': 'https://mcp.canva.com/mcp'})

    def test_bad_remote_entry_is_a_400(self):
        body, status = self._post({'name': 'canva', 'type': 'http',
                                   'url': 'http://example.com/mcp'})
        self.assertEqual(status, 400)
        self.assertIn('https', body['error'])


class CodexSyncTests(RegistryTestCase):
    def _which(self, name):
        return '/usr/bin/codex' if name == 'codex' else None

    def test_codex_add_and_remove_argv(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append((argv, kw))
            return mock.Mock(returncode=0)

        mr.set_server('svc', 'npx', args=['-y', 'pkg'], env={'K': 'v'})
        with mock.patch.object(mr.shutil, 'which', side_effect=self._which), \
             mock.patch.object(mr.subprocess, 'run', side_effect=fake_run):
            results = mr.sync_all()
        self.assertEqual(results['codex'], 'ok')
        argv = calls[0][0]
        self.assertEqual(argv, ['codex', 'mcp', 'add', 'svc', '--env', 'K=v',
                                '--', 'npx', '-y', 'pkg'])
        self.assertEqual(calls[0][1]['env']['CODEX_HOME'], mr.CODEX_HOME)

        calls.clear()
        mr.delete_server('svc')
        with mock.patch.object(mr.shutil, 'which', side_effect=self._which), \
             mock.patch.object(mr.subprocess, 'run', side_effect=fake_run):
            mr.sync_all()
        self.assertIn((['codex', 'mcp', 'remove', 'svc'],),
                      [(c[0],) for c in calls])

    def test_codex_absent_is_a_skip_not_a_crash(self):
        mr.set_server('svc', 'run')
        results = mr.sync_all()
        self.assertIn('codex CLI not installed', results['codex'])
        self.assertTrue(results['codex'].startswith('skipped'))


class ManagedListTests(RegistryTestCase):
    def test_failed_removal_is_retried_on_next_sync(self):
        mr.set_server('svc', 'run')
        mr.sync_all()
        mr.delete_server('svc')
        # First cleanup pass: claude config unwritable → removal must be kept
        # in `managed` for a retry.
        with mock.patch.object(mr, '_sync_claude', side_effect=OSError('disk')):
            results = mr.sync_all()
        self.assertIn('error', results['claude'])
        self.assertIn('svc', mr._read_registry()['managed'])
        self.assertIn('svc', self.read(self.claude_cfg)['mcpServers'])
        # Second pass succeeds and completes the cleanup.
        mr.sync_all()
        self.assertNotIn('svc', self.read(self.claude_cfg)['mcpServers'])
        self.assertNotIn('svc', mr._read_registry()['managed'])


class CliTests(RegistryTestCase):
    def test_main_sync_reports_and_exits_zero(self):
        mr.set_server('svc', 'run')
        with mock.patch('builtins.print') as p:
            rc = mr.main(['--sync'])
        self.assertEqual(rc, 0)
        printed = ' '.join(str(c) for c in p.call_args_list)
        self.assertIn('claude', printed)

    def test_main_without_sync_flag_usage_errors(self):
        self.assertEqual(mr.main([]), 2)


if __name__ == '__main__':
    unittest.main()
