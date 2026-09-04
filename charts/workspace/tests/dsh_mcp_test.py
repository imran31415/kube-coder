"""MCP servers for the DeepSeek Harness, over ACP `session/new` (#639).

Every other assistant reaches the workspace's MCP tools somehow — Claude via
`--mcp-config`, ante/opencode/codex via a seeded config file. The harness has
no config file we write; ACP takes the server list as a request field instead.
Without this the Hypervisor preamble tells a dsh thread it has `dashboard`
tools while it has none.

Run with:  python3 -m unittest tests.dsh_mcp_test  (from charts/workspace/)
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import acp_bridge  # noqa: E402
import hypervisor_session as hs  # noqa: E402
import mcp_agent_orchestrator as orch  # noqa: E402
import server  # noqa: E402

from acp_bridge_test import (  # noqa: E402
    STUB_AGENT, _base_scenario, _chunk,
)

DSH = 'deepseek-harness'


class CuratedSetTest(unittest.TestCase):
    def test_it_is_exactly_what_the_hypervisor_pins_for_claude(self):
        # Mirrored constants, same lockstep discipline as EFFORT_CAP: they live
        # in separate modules because the bridge must not import the session.
        self.assertEqual(
            acp_bridge.CURATED_MCP,
            json.loads(hs._HYPERVISOR_MCP_CONFIG)['mcpServers'])

    def test_it_is_the_curated_pair_not_the_full_boot_set(self):
        # ACP connects every declared server before publishing the session, and
        # a connection failure rolls the whole session back — so one slow
        # npx-launched server would be a dead session, not a missing tool.
        self.assertEqual(set(acp_bridge.CURATED_MCP), {'dashboard', 'memory'})


class ParseMcpTest(unittest.TestCase):
    def test_default_expands_to_acp_shape(self):
        got = acp_bridge.parse_mcp('default')
        self.assertEqual([s['name'] for s in got], ['dashboard', 'memory'])
        # Absolute — the harness rejects the session otherwise; see
        # test_the_command_is_made_absolute.
        self.assertEqual(got[0]['command'], shutil.which('python3'))
        self.assertEqual(got[0]['args'], ['/tmp/browser/mcp_dashboard.py'])
        # ACP wants env as name/value PAIRS, not a mapping.
        self.assertEqual(got[0]['env'], [])

    def test_accepts_the_repos_own_mcp_config_shape(self):
        got = acp_bridge.parse_mcp(json.dumps({'mcpServers': {
            'thing': {'type': 'stdio', 'command': 'python3',
                      'args': ['/x/y.py'], 'env': {'A': '1'}}}}))
        self.assertEqual(got, [{'name': 'thing',
                                'command': shutil.which('python3'),
                                'args': ['/x/y.py'],
                                'env': [{'name': 'A', 'value': '1'}]}])

    def test_accepts_a_bare_server_map_too(self):
        got = acp_bridge.parse_mcp(json.dumps(
            {'thing': {'command': 'python3', 'args': []}}))
        self.assertEqual([s['name'] for s in got], ['thing'])

    def test_empty_means_no_servers(self):
        self.assertEqual(acp_bridge.parse_mcp(''), [])
        self.assertEqual(acp_bridge.parse_mcp('   '), [])

    def test_non_stdio_entries_are_skipped_not_guessed(self):
        got = acp_bridge.parse_mcp(json.dumps({'mcpServers': {
            'remote': {'type': 'http', 'url': 'https://example.test/mcp'},
            'local': {'command': 'python3'}}}))
        self.assertEqual([s['name'] for s in got], ['local'])

    def test_malformed_input_degrades_to_no_servers(self):
        # An agent with no tools is a worse turn; an agent that never starts is
        # no turn at all.
        for bad in ('{not json', '[]', '"nope"', '{"mcpServers": 3}'):
            with self.subTest(spec=bad):
                self.assertEqual(acp_bridge.parse_mcp(bad), [])

    def test_a_junk_entry_does_not_lose_its_neighbours(self):
        got = acp_bridge.parse_mcp(json.dumps({'mcpServers': {
            'bad': 'not-a-dict', 'nocmd': {'args': ['x']},
            'good': {'command': 'python3', 'args': ['ok.py']}}}))
        self.assertEqual([s['name'] for s in got], ['good'])

    def test_the_command_is_made_absolute(self):
        # The harness REJECTS THE WHOLE SESSION on a relative command
        # ("mcpServers[0].command must be an absolute path"), while every
        # config in this repo spells it `python3` because the other harnesses
        # resolve it on PATH. Verified against a real `dsh --profile acp`.
        got = acp_bridge.parse_mcp('default')
        for srv in got:
            with self.subTest(server=srv['name']):
                self.assertTrue(os.path.isabs(srv['command']), srv['command'])
                self.assertTrue(srv['command'].endswith('python3'))

    def test_an_already_absolute_command_is_left_alone(self):
        got = acp_bridge.parse_mcp(json.dumps({'mcpServers': {
            'x': {'command': '/opt/custom/bin/thing'}}}))
        self.assertEqual(got[0]['command'], '/opt/custom/bin/thing')

    def test_an_unresolvable_command_drops_only_that_server(self):
        # Dropping one server beats a rejected session/new, which costs the
        # whole turn.
        got = acp_bridge.parse_mcp(json.dumps({'mcpServers': {
            'ghost': {'command': 'definitely-not-on-path-xyz'},
            'real': {'command': 'python3'}}}))
        self.assertEqual([s['name'] for s in got], ['real'])

    def test_odd_types_are_coerced_rather_than_raising(self):
        got = acp_bridge.parse_mcp(json.dumps({'mcpServers': {
            'x': {'command': 'python3', 'args': ['a', 1, True],
                  'env': {'N': 2}}}}))
        self.assertEqual(got[0]['args'], ['a', '1', 'True'])
        self.assertEqual(got[0]['env'], [{'name': 'N', 'value': '2'}])


class SessionWiringTest(unittest.TestCase):
    """The servers must actually reach session/new — and session/resume, or a
    second turn would silently lose every tool."""

    def _run(self, extra_args, scenario=None):
        tmp = tempfile.mkdtemp(prefix='dsh-mcp-')
        scenario_path = os.path.join(tmp, 'scenario.json')
        agent_path = os.path.join(tmp, 'stub_agent.py')
        recorded = os.path.join(tmp, 'recorded.jsonl')
        with open(scenario_path, 'w') as f:
            json.dump(scenario or _base_scenario(
                {'emit': [_chunk('ok')], 'result': {'stopReason': 'end_turn'}}), f)
        with open(agent_path, 'w') as f:
            f.write(STUB_AGENT)
        bridge = os.path.join(os.path.dirname(HERE), 'acp_bridge.py')
        env = dict(os.environ)
        env['KC_DSH_ARGV'] = json.dumps(
            [sys.executable, agent_path, scenario_path, recorded])
        env['KC_DSH_HANDSHAKE_TIMEOUT'] = '20'
        proc = subprocess.run([sys.executable, bridge, '--cwd', tmp, *extra_args],
                              input='go', capture_output=True, text=True,
                              env=env, timeout=60)
        sent = []
        if os.path.exists(recorded):
            with open(recorded) as f:
                sent = [json.loads(x) for x in f if x.strip()]
        return proc, sent

    @staticmethod
    def _call(sent, method):
        return next((m for m in sent if m.get('method') == method), None)

    def test_session_new_carries_the_servers(self):
        proc, sent = self._run(['--mcp', 'default'])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        params = self._call(sent, 'session/new')['params']
        self.assertEqual([s['name'] for s in params['mcpServers']],
                         ['dashboard', 'memory'])

    def test_session_resume_carries_them_too(self):
        scenario = _base_scenario({'emit': [_chunk('ok')],
                                   'result': {'stopReason': 'end_turn'}})
        scenario['session/resume'] = {'result': {}}
        proc, sent = self._run(['--mcp', 'default', '--session', 'sess-1'],
                               scenario)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        params = self._call(sent, 'session/resume')['params']
        self.assertEqual([s['name'] for s in params['mcpServers']],
                         ['dashboard', 'memory'])

    def test_omitting_mcp_sends_an_empty_list(self):
        proc, sent = self._run([])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._call(sent, 'session/new')['params']['mcpServers'],
                         [])


class CallSiteTest(unittest.TestCase):
    def test_the_hypervisor_adapter_passes_the_pinned_config(self):
        a = hs.DeepseekHarnessAdapter()
        argv = a.build({'workdir': '/home/dev'}, 'hi', first=True)['argv']
        spec = argv[argv.index('--mcp') + 1]
        self.assertEqual(json.loads(spec), json.loads(hs._HYPERVISOR_MCP_CONFIG))

    def test_the_build_command_asks_for_the_curated_set(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            cmd = server.ClaudeTaskManager.assistant_command(DSH)
        argv = shlex.split(cmd)
        self.assertEqual(argv[argv.index('--mcp') + 1], 'default')

    def test_the_subagent_command_asks_for_the_curated_set(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            headless = orch._assistant_command(DSH, 'go', headless=True)
            interactive = orch._assistant_command(DSH, '', headless=False)
        for cmd in (headless, interactive):
            with self.subTest(cmd=cmd):
                argv = shlex.split(cmd.partition('|')[2] or cmd)
                self.assertEqual(argv[argv.index('--mcp') + 1], 'default')

    def test_every_surface_gets_tools(self):
        # The preamble tells the agent it has `dashboard` tools; all three
        # launch paths must actually give it some.
        with mock.patch.dict(os.environ, {}, clear=True):
            build = server.ClaudeTaskManager.assistant_command(DSH)
            sub = orch._assistant_command(DSH, 'go', headless=True)
        thread = hs.DeepseekHarnessAdapter().build({}, 'hi', first=True)['argv']
        self.assertIn('--mcp', build)
        self.assertIn('--mcp', sub)
        self.assertIn('--mcp', thread)


if __name__ == '__main__':
    unittest.main()
