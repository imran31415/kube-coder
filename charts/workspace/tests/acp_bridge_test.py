"""Unit tests for charts/workspace/acp_bridge.py (issue #639).

Two layers, deliberately:

  * Pure-function tests for the mapping helpers — content blocks, tool-call
    content, config-option matching, error text.
  * End-to-end tests that spawn a REAL subprocess speaking REAL ACP JSON-RPC
    over stdio (`_StubAgent` below) and assert on the bridge's stdout stream
    and exit code. The whole point of the bridge is bidirectional protocol
    handling — request/response correlation, answering the agent mid-turn,
    merging tool-call updates — and none of that is exercised by calling
    methods on an object. No `dsh` binary is needed.

The stub's handshake response is the one captured verbatim from a real
`dsh --profile acp` 0.1.2-rc.1 in-pod, including its grouped, JSON-encoded
model values — the shape `_match_config_value` exists to cope with.

Run with:    python3 -m unittest tests.acp_bridge_test
(from charts/workspace/)
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import acp_bridge  # noqa: E402


# Captured verbatim from `dsh --profile acp` 0.1.2-rc.1 (Node 22, in-pod).
REAL_INITIALIZE_RESULT = {
    'protocolVersion': 1,
    'agentInfo': {'name': 'deepseek-harness-acp', 'version': '0.0.1'},
    'agentCapabilities': {
        'mcpCapabilities': {'http': True},
        'promptCapabilities': {'image': False, 'audio': False,
                               'embeddedContext': False},
        'sessionCapabilities': {'close': {}, 'list': {}, 'resume': {}},
    },
    'authMethods': [],
}

# Also captured verbatim: note the grouped select and the JSON-encoded
# ["provider","model"] values.
REAL_CONFIG_OPTIONS = [
    {
        'id': 'model', 'name': 'Model', 'category': 'model', 'type': 'select',
        'currentValue': '["deepseek-official","deepseek-v4-flash"]',
        'options': [{
            'group': 'deepseek-official', 'name': 'DeepSeek',
            'options': [
                {'value': '["deepseek-official","deepseek-v4-flash"]',
                 'name': 'DeepSeek-V4-Flash',
                 'description': 'Fast, efficient, and economical; suited to '
                                'focused, routine, or parallel tasks.'},
                {'value': '["deepseek-official","deepseek-v4-pro"]',
                 'name': 'DeepSeek-V4-Pro',
                 'description': 'Stronger agentic coding, knowledge, and '
                                'difficult reasoning; suited to complex or '
                                'quality-critical tasks at higher cost.'},
                {'value': '["deepseek-official","deepseek-v4-flash-vision-exp"]',
                 'name': 'DeepSeek-V4-Flash-Vision-Exp'},
            ],
        }],
    },
    {
        'id': 'reasoning_effort', 'name': 'Reasoning effort',
        'category': 'thought_level', 'type': 'select', 'currentValue': 'high',
        'options': [
            {'value': 'off', 'name': 'Off'},
            {'value': 'low', 'name': 'Low'},
            {'value': 'high', 'name': 'High'},
            {'value': 'max', 'name': 'Max'},
        ],
    },
]

SESSION_ID = 'eaf27528-e856-4ec9-b664-d974579e14fe'

# A scriptable ACP agent. Reads JSON-RPC from stdin; for each request it looks
# up a canned reply in the scenario file and, before replying, emits any
# `session/update` notifications (or client-bound requests) that scenario step
# lists. That ordering is what a real agent does: updates stream, then the
# prompt response settles the turn.
STUB_AGENT = textwrap.dedent('''
    import json, sys
    scenario = json.load(open(sys.argv[1]))
    recorded = open(sys.argv[2], 'w')

    def send(o):
        sys.stdout.write(json.dumps(o) + '\\n')
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        recorded.write(line + '\\n')
        recorded.flush()
        method = msg.get('method')
        if method is None:
            continue  # a response to something we asked the client
        step = scenario.get(method)
        if step is None:
            if 'id' in msg:
                send({'jsonrpc': '2.0', 'id': msg['id'],
                      'error': {'code': -32601, 'message': 'no stub for ' + method}})
            continue
        for out in step.get('emit', []):
            send(out)
        if 'id' in msg:
            if 'error' in step:
                send({'jsonrpc': '2.0', 'id': msg['id'], 'error': step['error']})
            else:
                send({'jsonrpc': '2.0', 'id': msg['id'],
                      'result': step.get('result', {})})
        if step.get('exit'):
            break
''')


def _update(payload):
    return {'jsonrpc': '2.0', 'method': 'session/update',
            'params': {'sessionId': SESSION_ID, 'update': payload}}


def _chunk(text, kind='agent_message_chunk', message_id='m1'):
    return _update({'sessionUpdate': kind, 'messageId': message_id,
                    'content': {'type': 'text', 'text': text}})


def _base_scenario(prompt_step):
    return {
        'initialize': {'result': REAL_INITIALIZE_RESULT},
        'session/new': {'result': {'sessionId': SESSION_ID,
                                   'configOptions': REAL_CONFIG_OPTIONS}},
        'session/set_config_option': {'result': {'configOptions': REAL_CONFIG_OPTIONS}},
        'session/close': {'result': {}},
        'session/prompt': prompt_step,
    }


class _Run:
    """Result of driving the bridge end to end against the stub."""

    def __init__(self, rc, events, sent, stderr):
        self.rc = rc
        self.events = events
        self.sent = sent          # every JSON-RPC message the client sent
        self.stderr = stderr

    def of_type(self, t):
        return [e for e in self.events if e.get('type') == t]

    def first(self, t):
        got = self.of_type(t)
        return got[0] if got else None

    def sent_methods(self):
        return [m.get('method') for m in self.sent if m.get('method')]

    def sent_call(self, method):
        for m in self.sent:
            if m.get('method') == method:
                return m
        return None


class AcpBridgeE2ETest(unittest.TestCase):
    """Drives acp_bridge.main() as a subprocess against the stub agent."""

    maxDiff = None

    def run_bridge_with(self, scenario, prompt='do the thing', extra_args=(),
                        timeout=60):
        """Spawn the bridge with KC_DSH_ARGV pointed at the stub agent, feed
        it `prompt` on stdin, and collect both sides of the conversation."""
        tmp = tempfile.mkdtemp(prefix='acp-bridge-test-')
        scenario_path = os.path.join(tmp, 'scenario.json')
        recorded_path = os.path.join(tmp, 'recorded.jsonl')
        agent_path = os.path.join(tmp, 'stub_agent.py')
        with open(scenario_path, 'w') as f:
            json.dump(scenario, f)
        with open(agent_path, 'w') as f:
            f.write(STUB_AGENT)

        bridge_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'acp_bridge.py')
        env = dict(os.environ)
        env['KC_DSH_ARGV'] = json.dumps(
            [sys.executable, agent_path, scenario_path, recorded_path])
        env['KC_DSH_HANDSHAKE_TIMEOUT'] = '20'
        proc = subprocess.run(
            [sys.executable, bridge_path, '--cwd', tmp, *extra_args],
            input=prompt, capture_output=True, text=True, env=env,
            timeout=timeout)
        events = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
        sent = []
        if os.path.exists(recorded_path):
            with open(recorded_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        sent.append(json.loads(line))
        return _Run(proc.returncode, events, sent, proc.stderr)

    # ── happy path ──────────────────────────────────────────────────────

    def test_plain_turn_streams_one_message_not_one_per_chunk(self):
        scenario = _base_scenario({
            'emit': [_chunk('Hello'), _chunk(', '), _chunk('world.')],
            'result': {'stopReason': 'end_turn'},
        })
        run = self.run_bridge_with(scenario)
        self.assertEqual(run.rc, 0, run.stderr)
        self.assertEqual([e['type'] for e in run.events],
                         ['session', 'message', 'done'])
        self.assertEqual(run.first('message')['text'], 'Hello, world.')
        self.assertEqual(run.first('session')['sessionId'], SESSION_ID)
        self.assertEqual(run.first('done')['stopReason'], 'end_turn')

    def test_handshake_uses_protocol_1_and_understates_capabilities(self):
        # Understating is load-bearing: the agent must not ask us for a file
        # read or a terminal, because an unanswerable request hangs the turn.
        run = self.run_bridge_with(_base_scenario(
            {'result': {'stopReason': 'end_turn'}}))
        init = run.sent_call('initialize')
        self.assertEqual(init['params']['protocolVersion'], 1)
        caps = init['params']['clientCapabilities']
        self.assertEqual(caps['fs'], {'readTextFile': False,
                                      'writeTextFile': False})
        self.assertFalse(caps['terminal'])
        self.assertIn('session/new', run.sent_methods())
        self.assertIn('session/prompt', run.sent_methods())

    def test_prompt_carries_the_stdin_text_as_a_text_block(self):
        run = self.run_bridge_with(
            _base_scenario({'result': {'stopReason': 'end_turn'}}),
            prompt='multi\nline\nprompt with $quotes and `backticks`')
        sent = run.sent_call('session/prompt')
        self.assertEqual(sent['params']['sessionId'], SESSION_ID)
        self.assertEqual(sent['params']['prompt'],
                         [{'type': 'text',
                           'text': 'multi\nline\nprompt with $quotes and `backticks`'}])

    # ── tool cards ──────────────────────────────────────────────────────

    def test_tool_call_then_update_becomes_a_call_and_a_result(self):
        scenario = _base_scenario({
            'emit': [
                _chunk('Let me look.'),
                _update({'sessionUpdate': 'tool_call', 'toolCallId': 't1',
                         'title': 'Read README.md', 'name': 'read_file',
                         'kind': 'read', 'status': 'pending',
                         'rawInput': {'path': 'README.md'}}),
                _update({'sessionUpdate': 'tool_call_update', 'toolCallId': 't1',
                         'status': 'completed',
                         'content': [{'type': 'content',
                                      'content': {'type': 'text',
                                                  'text': '# kube-coder'}}]}),
                _chunk('It is the readme.', message_id='m2'),
            ],
            'result': {'stopReason': 'end_turn'},
        })
        run = self.run_bridge_with(scenario)
        self.assertEqual(run.rc, 0, run.stderr)
        self.assertEqual([e['type'] for e in run.events],
                         ['session', 'message', 'tool_call', 'tool_result',
                          'message', 'done'])
        call = run.first('tool_call')
        self.assertEqual(call['id'], 't1')
        self.assertEqual(call['name'], 'read_file')
        self.assertEqual(call['kind'], 'read')
        self.assertEqual(call['input'], {'path': 'README.md'})
        result = run.first('tool_result')
        self.assertEqual(result['id'], 't1')
        self.assertFalse(result['is_error'])
        self.assertEqual(result['text'], '# kube-coder')
        # Ordering matters: text before the tool must not be swallowed into the
        # text after it.
        msgs = [e['text'] for e in run.of_type('message')]
        self.assertEqual(msgs, ['Let me look.', 'It is the readme.'])

    def test_failed_tool_is_flagged_is_error(self):
        scenario = _base_scenario({
            'emit': [
                _update({'sessionUpdate': 'tool_call', 'toolCallId': 't9',
                         'title': 'Run tests', 'name': 'bash',
                         'status': 'in_progress'}),
                _update({'sessionUpdate': 'tool_call_update', 'toolCallId': 't9',
                         'status': 'failed', 'rawOutput': 'exit 1'}),
            ],
            'result': {'stopReason': 'end_turn'},
        })
        run = self.run_bridge_with(scenario)
        result = run.first('tool_result')
        self.assertTrue(result['is_error'])
        self.assertEqual(result['text'], 'exit 1')

    def test_tool_call_that_arrives_already_completed_still_reports_once(self):
        scenario = _base_scenario({
            'emit': [
                _update({'sessionUpdate': 'tool_call', 'toolCallId': 't2',
                         'title': 'Grep', 'name': 'grep', 'status': 'completed',
                         'content': [{'type': 'content',
                                      'content': {'type': 'text', 'text': 'hit'}}]}),
                # A late duplicate update must not double-render the card.
                _update({'sessionUpdate': 'tool_call_update', 'toolCallId': 't2',
                         'status': 'completed'}),
            ],
            'result': {'stopReason': 'end_turn'},
        })
        run = self.run_bridge_with(scenario)
        self.assertEqual(len(run.of_type('tool_call')), 1)
        self.assertEqual(len(run.of_type('tool_result')), 1)
        self.assertEqual(run.first('tool_result')['text'], 'hit')

    def test_update_for_an_unseen_tool_call_still_renders_a_card(self):
        scenario = _base_scenario({
            'emit': [
                _update({'sessionUpdate': 'tool_call_update', 'toolCallId': 't3',
                         'title': 'Edit main.py', 'name': 'edit',
                         'status': 'completed',
                         'content': [{'type': 'diff', 'path': '/x/main.py',
                                      'newText': 'y'}]}),
            ],
            'result': {'stopReason': 'end_turn'},
        })
        run = self.run_bridge_with(scenario)
        self.assertEqual(len(run.of_type('tool_call')), 1)
        self.assertEqual(run.first('tool_call')['name'], 'edit')
        self.assertEqual(run.first('tool_result')['text'], '[diff /x/main.py]')

    def test_thought_chunks_are_a_separate_event_kind(self):
        scenario = _base_scenario({
            'emit': [_chunk('thinking...', kind='agent_thought_chunk'),
                     _chunk('answer', message_id='m2')],
            'result': {'stopReason': 'end_turn'},
        })
        run = self.run_bridge_with(scenario)
        self.assertEqual([e['type'] for e in run.events],
                         ['session', 'thought', 'message', 'done'])

    def test_usage_update_is_forwarded_and_noise_updates_are_not(self):
        scenario = _base_scenario({
            'emit': [
                _update({'sessionUpdate': 'user_message_chunk',
                         'content': {'type': 'text', 'text': 'echo of my prompt'}}),
                _update({'sessionUpdate': 'plan', 'entries': []}),
                _update({'sessionUpdate': 'usage_update', 'used': 1200,
                         'size': 128000}),
            ],
            'result': {'stopReason': 'end_turn'},
        })
        run = self.run_bridge_with(scenario)
        self.assertEqual([e['type'] for e in run.events],
                         ['session', 'usage', 'done'])
        self.assertEqual(run.first('usage'), {'type': 'usage', 'used': 1200,
                                              'size': 128000})

    # ── the bidirectional half ──────────────────────────────────────────

    def test_permission_request_is_auto_approved_so_the_turn_never_stalls(self):
        scenario = _base_scenario({
            'emit': [
                {'jsonrpc': '2.0', 'id': 900,
                 'method': 'session/request_permission',
                 'params': {
                     'sessionId': SESSION_ID,
                     'toolCall': {'toolCallId': 't1', 'title': 'rm -rf build'},
                     'options': [
                         {'optionId': 'rej', 'name': 'No', 'kind': 'reject_once'},
                         {'optionId': 'once', 'name': 'Yes', 'kind': 'allow_once'},
                         {'optionId': 'always', 'name': 'Always',
                          'kind': 'allow_always'},
                     ]}},
                _chunk('done'),
            ],
            'result': {'stopReason': 'end_turn'},
        })
        run = self.run_bridge_with(scenario)
        self.assertEqual(run.rc, 0, run.stderr)
        answers = [m for m in run.sent if m.get('id') == 900]
        self.assertEqual(len(answers), 1, 'the agent must get exactly one answer')
        self.assertEqual(answers[0]['result']['outcome'],
                         {'outcome': 'selected', 'optionId': 'always'})

    def test_unsupported_agent_request_is_answered_with_an_error_not_silence(self):
        # Silence here would hang the turn forever, which is strictly worse
        # than telling the agent we cannot do it.
        scenario = _base_scenario({
            'emit': [
                {'jsonrpc': '2.0', 'id': 901, 'method': 'fs/read_text_file',
                 'params': {'path': '/etc/shadow'}},
                _chunk('carried on'),
            ],
            'result': {'stopReason': 'end_turn'},
        })
        run = self.run_bridge_with(scenario)
        self.assertEqual(run.rc, 0, run.stderr)
        answers = [m for m in run.sent if m.get('id') == 901]
        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0]['error']['code'], -32601)
        self.assertEqual(run.first('message')['text'], 'carried on')

    # ── model / effort selection ────────────────────────────────────────

    def test_model_pick_is_resolved_against_the_advertised_options(self):
        run = self.run_bridge_with(
            _base_scenario({'result': {'stopReason': 'end_turn'}}),
            extra_args=('--model', 'deepseek-v4-pro', '--effort', 'max'))
        sets = [m for m in run.sent
                if m.get('method') == 'session/set_config_option']
        by_id = {m['params']['configId']: m['params']['value'] for m in sets}
        self.assertEqual(by_id['model'], '["deepseek-official","deepseek-v4-pro"]')
        self.assertEqual(by_id['reasoning_effort'], 'max')

    def test_unknown_model_keeps_the_harness_default_instead_of_failing(self):
        run = self.run_bridge_with(
            _base_scenario({'result': {'stopReason': 'end_turn'}}),
            extra_args=('--model', 'gpt-9-ultra'))
        self.assertEqual(run.rc, 0, run.stderr)
        self.assertNotIn('session/set_config_option', run.sent_methods())
        self.assertIn('not offered by this session', run.stderr)

    def test_a_failing_set_config_option_does_not_fail_the_turn(self):
        scenario = _base_scenario({'result': {'stopReason': 'end_turn'}})
        scenario['session/set_config_option'] = {
            'error': {'code': -32602, 'message': 'Invalid params: bad option'}}
        run = self.run_bridge_with(scenario, extra_args=('--model',
                                                         'deepseek-v4-pro'))
        self.assertEqual(run.rc, 0, run.stderr)
        self.assertEqual(run.first('done')['stopReason'], 'end_turn')

    # ── resume ──────────────────────────────────────────────────────────

    def test_resume_uses_session_resume_and_keeps_the_id(self):
        scenario = _base_scenario({'result': {'stopReason': 'end_turn'}})
        scenario['session/resume'] = {'result': {'configOptions': REAL_CONFIG_OPTIONS}}
        run = self.run_bridge_with(scenario, extra_args=('--session', SESSION_ID))
        self.assertIn('session/resume', run.sent_methods())
        self.assertNotIn('session/new', run.sent_methods())
        self.assertEqual(run.first('session')['sessionId'], SESSION_ID)

    def test_unresumable_session_falls_back_to_a_new_one(self):
        # A pruned session must cost the user their history, not their turn.
        scenario = _base_scenario({'result': {'stopReason': 'end_turn'}})
        scenario['session/resume'] = {
            'error': {'code': -32602, 'message': 'Invalid params: unknown session'}}
        run = self.run_bridge_with(scenario, extra_args=('--session', 'gone'))
        self.assertEqual(run.rc, 0, run.stderr)
        self.assertIn('session/new', run.sent_methods())
        self.assertEqual(run.first('session')['sessionId'], SESSION_ID)
        self.assertIn('starting a new session', run.stderr)

    # ── failure modes ───────────────────────────────────────────────────

    def test_prompt_error_becomes_an_error_event_and_a_nonzero_exit(self):
        # The real shape when DEEPSEEK_API_KEY is missing or wrong, captured
        # in-pod against dsh 0.1.2-rc.1.
        scenario = _base_scenario({
            'error': {'code': -32603,
                      'message': 'Internal error: turn failed: Authentication '
                                 'Fails, Your api key: ****0000 is invalid'}})
        run = self.run_bridge_with(scenario)
        self.assertEqual(run.rc, 1)
        self.assertIn('Authentication Fails', run.first('error')['text'])
        self.assertEqual(run.of_type('done'), [])

    def test_session_new_failure_is_reported_and_exits_nonzero(self):
        scenario = _base_scenario({'result': {'stopReason': 'end_turn'}})
        scenario['session/new'] = {'error': {'code': -32603,
                                             'message': 'workspace not found'}}
        run = self.run_bridge_with(scenario)
        self.assertEqual(run.rc, 1)
        self.assertIn('session/new failed', run.first('error')['text'])

    def test_agent_that_dies_mid_turn_yields_an_error_not_a_hang(self):
        scenario = _base_scenario({'result': {'stopReason': 'end_turn'}})
        scenario['session/new'] = {'exit': True}
        run = self.run_bridge_with(scenario, timeout=60)
        self.assertEqual(run.rc, 1)
        self.assertTrue(run.of_type('error'))

    def test_missing_binary_reports_cleanly(self):
        env = dict(os.environ)
        env['KC_DSH_BIN'] = '/nonexistent/dsh-does-not-exist'
        env.pop('KC_DSH_ARGV', None)
        bridge_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'acp_bridge.py')
        proc = subprocess.run([sys.executable, bridge_path], input='hi',
                              capture_output=True, text=True, env=env,
                              timeout=60)
        self.assertEqual(proc.returncode, 1)
        events = [json.loads(x) for x in proc.stdout.splitlines() if x.strip()]
        self.assertEqual(events[0]['type'], 'error')
        self.assertIn('cannot start', events[0]['text'])

    def test_empty_prompt_is_rejected_without_spawning_anything(self):
        bridge_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'acp_bridge.py')
        env = dict(os.environ)
        env['KC_DSH_BIN'] = '/nonexistent/dsh-does-not-exist'
        env.pop('KC_DSH_ARGV', None)
        proc = subprocess.run([sys.executable, bridge_path], input='   \n',
                              capture_output=True, text=True, env=env,
                              timeout=60)
        self.assertEqual(proc.returncode, 2)
        events = [json.loads(x) for x in proc.stdout.splitlines() if x.strip()]
        self.assertEqual(events, [{'type': 'error', 'text': 'empty prompt'}])

    def test_non_json_noise_on_the_agents_stdout_is_survived(self):
        scenario = _base_scenario({
            'emit': [_chunk('still here')],
            'result': {'stopReason': 'end_turn'},
        })
        # A bare string is not a JSON-RPC object; the bridge must log and
        # continue rather than tear down a working turn.
        scenario['session/prompt']['emit'].insert(0, 'not json at all')
        run = self.run_bridge_with(scenario)
        self.assertEqual(run.rc, 0, run.stderr)
        self.assertEqual(run.first('message')['text'], 'still here')

    def test_refusal_and_cancellation_are_outcomes_not_failures(self):
        for stop in ('refusal', 'cancelled', 'max_tokens'):
            with self.subTest(stop=stop):
                run = self.run_bridge_with(_base_scenario(
                    {'emit': [_chunk('nope')], 'result': {'stopReason': stop}}))
                self.assertEqual(run.rc, 0, run.stderr)
                self.assertEqual(run.first('done')['stopReason'], stop)


class ServeModeTest(unittest.TestCase):
    """Serve mode is what the Builds tab runs: one long-lived ACP session fed
    prompt after prompt from a tmux pane that never sends EOF."""

    def _spawn(self, scenario, extra_args=()):
        tmp = tempfile.mkdtemp(prefix='acp-serve-test-')
        scenario_path = os.path.join(tmp, 'scenario.json')
        recorded_path = os.path.join(tmp, 'recorded.jsonl')
        agent_path = os.path.join(tmp, 'stub_agent.py')
        with open(scenario_path, 'w') as f:
            json.dump(scenario, f)
        with open(agent_path, 'w') as f:
            f.write(STUB_AGENT)
        bridge_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'acp_bridge.py')
        env = dict(os.environ)
        env['KC_DSH_ARGV'] = json.dumps(
            [sys.executable, agent_path, scenario_path, recorded_path])
        env['KC_DSH_HANDSHAKE_TIMEOUT'] = '20'
        proc = subprocess.Popen(
            [sys.executable, bridge_path, '--cwd', tmp, '--serve', *extra_args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env, bufsize=1)
        return proc, recorded_path

    @staticmethod
    def _collect(proc, recorded_path, timeout=90):
        # A test that already closed stdin must not have communicate() try to
        # flush it again.
        if proc.stdin is not None and proc.stdin.closed:
            proc.stdin = None
        out, err = proc.communicate(timeout=timeout)
        events, plain = [], []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                plain.append(line)
        sent = []
        if os.path.exists(recorded_path):
            with open(recorded_path) as f:
                for line in f:
                    if line.strip():
                        sent.append(json.loads(line))
        return proc.returncode, events, plain, sent, err

    def test_two_prompts_reuse_one_session(self):
        # Booting dsh's plugin tree is the slow part of a turn, and a fresh
        # session per prompt would also throw away the conversation.
        proc, rec = self._spawn(_base_scenario(
            {'emit': [_chunk('ok')], 'result': {'stopReason': 'end_turn'}}))
        proc.stdin.write('first prompt\n')
        proc.stdin.flush()
        time.sleep(4)
        proc.stdin.write('second prompt\n')
        proc.stdin.flush()
        time.sleep(4)
        proc.stdin.close()
        rc, events, _plain, sent, err = self._collect(proc, rec)
        self.assertEqual(rc, 0, err)
        methods = [m.get('method') for m in sent if m.get('method')]
        self.assertEqual(methods.count('session/new'), 1)
        self.assertEqual(methods.count('session/prompt'), 2)
        self.assertEqual(methods.count('initialize'), 1)
        texts = [m['params']['prompt'][0]['text']
                 for m in sent if m.get('method') == 'session/prompt']
        self.assertEqual([t.strip() for t in texts],
                         ['first prompt', 'second prompt'])
        self.assertEqual(len([e for e in events if e.get('type') == 'done']), 2)

    def test_closed_stdin_exits_zero(self):
        proc, rec = self._spawn(_base_scenario(
            {'emit': [_chunk('ok')], 'result': {'stopReason': 'end_turn'}}))
        proc.stdin.write('only prompt\n')
        proc.stdin.flush()
        time.sleep(4)
        proc.stdin.close()
        rc, _events, _plain, _sent, err = self._collect(proc, rec)
        self.assertEqual(rc, 0, err)

    def test_exit_word_closes_the_pane_cleanly(self):
        proc, rec = self._spawn(_base_scenario(
            {'emit': [_chunk('ok')], 'result': {'stopReason': 'end_turn'}}))
        proc.stdin.write('hello\n')
        proc.stdin.flush()
        time.sleep(4)
        proc.stdin.write('/exit\n')
        proc.stdin.flush()
        rc, _events, _plain, sent, err = self._collect(proc, rec)
        self.assertEqual(rc, 0, err)
        methods = [m.get('method') for m in sent if m.get('method')]
        self.assertEqual(methods.count('session/prompt'), 1)

    def test_a_failed_turn_does_not_end_the_session(self):
        # The user should be able to fix the key (or the prompt) and retry in
        # the same pane.
        scenario = _base_scenario({
            'error': {'code': -32603, 'message': 'Internal error: turn failed: '
                                                 'Authentication Fails'}})
        proc, rec = self._spawn(scenario)
        proc.stdin.write('one\n')
        proc.stdin.flush()
        time.sleep(4)
        proc.stdin.write('two\n')
        proc.stdin.flush()
        time.sleep(4)
        proc.stdin.close()
        rc, events, _plain, sent, err = self._collect(proc, rec)
        self.assertEqual(rc, 0, err)
        methods = [m.get('method') for m in sent if m.get('method')]
        self.assertEqual(methods.count('session/prompt'), 2)
        self.assertEqual(len([e for e in events if e.get('type') == 'error']), 2)

    def test_stream_json_format_renders_for_a_tmux_pane(self):
        scenario = _base_scenario({
            'emit': [
                _chunk('Looking.'),
                _update({'sessionUpdate': 'tool_call', 'toolCallId': 't1',
                         'title': 'Read README.md', 'name': 'read_file',
                         'status': 'pending', 'rawInput': {'path': 'README.md'}}),
                _update({'sessionUpdate': 'tool_call_update', 'toolCallId': 't1',
                         'status': 'completed',
                         'content': [{'type': 'content',
                                      'content': {'type': 'text',
                                                  'text': '# kube-coder'}}]}),
                _chunk('It is the readme.', message_id='m2'),
            ],
            'result': {'stopReason': 'end_turn'},
        })
        proc, rec = self._spawn(scenario, extra_args=('--format', 'stream-json'))
        proc.stdin.write('read the readme\n')
        proc.stdin.flush()
        time.sleep(4)
        proc.stdin.close()
        rc, events, plain, _sent, err = self._collect(proc, rec)
        self.assertEqual(rc, 0, err)
        kinds = [(e.get('type'),
                  (e.get('message', {}).get('content') or [{}])[0].get('type'))
                 for e in events]
        self.assertEqual(kinds, [
            ('assistant', 'text'),
            ('assistant', 'tool_use'),
            ('user', 'tool_result'),
            ('assistant', 'text'),
            ('result', None),
        ])
        # The tool blocks use the same field names the Claude log parser reads,
        # so a Build transcript renders with no special-casing.
        tool_use = events[1]['message']['content'][0]
        self.assertEqual(tool_use['name'], 'read_file')
        self.assertEqual(tool_use['input'], {'path': 'README.md'})
        self.assertEqual(tool_use['id'], 't1')
        tool_result = events[2]['message']['content'][0]
        self.assertEqual(tool_result['tool_use_id'], 't1')
        self.assertEqual(tool_result['content'], '# kube-coder')
        self.assertFalse(tool_result['is_error'])
        # The closing result carries the answer, not an empty string.
        self.assertEqual(events[-1]['result'], 'It is the readme.')
        # Human-readable lines share stdout, and none of them may start with
        # `{` or the dashboard's JSON filter would try to parse them.
        self.assertTrue(plain)
        self.assertFalse([p for p in plain if p.startswith('{')])

    def test_stream_json_reports_a_failed_turn_once(self):
        scenario = _base_scenario({
            'error': {'code': -32603, 'message': 'turn failed: no key'}})
        proc, rec = self._spawn(scenario, extra_args=('--format', 'stream-json'))
        proc.stdin.write('go\n')
        proc.stdin.flush()
        time.sleep(4)
        proc.stdin.close()
        rc, events, _plain, _sent, err = self._collect(proc, rec)
        self.assertEqual(rc, 0, err)
        results = [e for e in events if e.get('type') == 'result']
        self.assertEqual(len(results), 1)
        self.assertIn('no key', results[0]['result'])


class StreamJsonSinkTest(unittest.TestCase):
    """Serve mode reuses ONE sink across every prompt, so its per-turn state
    has to be reset per turn. Driven directly: the failure only shows up in a
    turn that settles without saying anything, which is awkward to script
    through a stub agent but trivial here."""

    def drive(self, steps):
        sink = acp_bridge.StreamJsonSink()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            for step in steps:
                if step == 'START':
                    sink.turn_start()
                elif step == 'END':
                    sink.turn_end()
                else:
                    sink.emit(step)
        out = []
        for line in buf.getvalue().splitlines():
            if line.startswith('{'):
                out.append(json.loads(line))
        return out

    def results(self, steps):
        return [e['result'] for e in self.drive(steps) if e.get('type') == 'result']

    def test_a_settled_turn_reports_its_own_answer(self):
        self.assertEqual(
            self.results(['START', {'type': 'message', 'text': 'hello'}, 'END']),
            ['hello'])

    def test_a_failed_turn_does_not_bleed_into_the_next_one(self):
        # The bug this exists for: a failed turn sets the result text and never
        # reaches turn_end, so without the per-turn reset the NEXT turn — if it
        # settles without saying anything, e.g. a tool-only turn — would close
        # by reporting the previous turn's error as its own outcome.
        self.assertEqual(self.results([
            'START', {'type': 'error', 'text': 'boom'},          # turn 1 fails
            'START',                                             # turn 2
            {'type': 'tool_call', 'id': 't1', 'name': 'bash', 'input': {}},
            {'type': 'tool_result', 'id': 't1', 'text': 'ok'},
            'END',
        ]), ['error: boom', ''])

    def test_an_answer_does_not_bleed_into_the_next_turn_either(self):
        self.assertEqual(self.results([
            'START', {'type': 'message', 'text': 'first answer'}, 'END',
            'START', {'type': 'tool_call', 'id': 't1', 'name': 'bash',
                      'input': {}}, 'END',
        ]), ['first answer', ''])

    def test_thoughts_are_shown_but_are_not_the_answer(self):
        # A `thought` is rendered for the watcher, but the turn's result is the
        # committed message — reporting reasoning as the answer would be worse
        # than reporting nothing.
        self.assertEqual(self.results([
            'START', {'type': 'thought', 'text': 'hmm'},
            {'type': 'message', 'text': 'the answer'}, 'END']),
            ['the answer'])

    def test_no_plain_line_can_be_mistaken_for_an_event(self):
        sink = acp_bridge.StreamJsonSink()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sink.turn_start()
            sink.emit({'type': 'session', 'sessionId': 's1'})
            sink.emit({'type': 'message', 'text': '{"looks": "like json"}'})
            sink.emit({'type': 'tool_call', 'id': 't', 'name': 'x', 'input': {}})
            sink.emit({'type': 'tool_result', 'id': 't', 'text': 'r'})
            sink.emit({'type': 'error', 'text': 'e'})
        for line in buf.getvalue().splitlines():
            if line.startswith('{'):
                json.loads(line)  # every {-leading line must parse as an event
            else:
                self.assertTrue(line[:1] in '·◇…↳⚒✗', line)


class ContentMappingTest(unittest.TestCase):
    def test_block_text_variants(self):
        self.assertEqual(acp_bridge._block_text({'type': 'text', 'text': 'hi'}),
                         'hi')
        self.assertEqual(
            acp_bridge._block_text({'type': 'resource_link', 'name': 'a.py',
                                    'uri': 'file:///a.py'}),
            '[resource_link a.py]')
        self.assertEqual(
            acp_bridge._block_text({'type': 'resource',
                                    'resource': {'text': 'inline'}}), 'inline')
        self.assertEqual(acp_bridge._block_text({'type': 'image'}), '[image]')
        # Unknown/malformed blocks must not raise.
        self.assertEqual(acp_bridge._block_text(None), '')
        self.assertEqual(acp_bridge._block_text({'type': 'quantum'}), '')

    def test_tool_content_flattens_mixed_items(self):
        text = acp_bridge._tool_content_text([
            {'type': 'content', 'content': {'type': 'text', 'text': 'line one'}},
            {'type': 'diff', 'path': '/a/b.py', 'newText': 'x'},
            {'type': 'terminal', 'terminalId': 'term-1'},
            'garbage',
        ])
        self.assertEqual(text, 'line one\n[diff /a/b.py]\n[terminal term-1]')
        self.assertEqual(acp_bridge._tool_content_text(None), '')

    def test_error_text_prefers_message_and_appends_data(self):
        self.assertEqual(
            acp_bridge._err_text({'error': {'code': -1, 'message': 'boom'}}),
            'boom')
        self.assertEqual(
            acp_bridge._err_text({'error': {'message': 'boom',
                                            'data': {'why': 'no key'}}}),
            'boom: {"why": "no key"}')
        self.assertEqual(acp_bridge._err_text({'error': None}), 'unknown error')


class ConfigMatchTest(unittest.TestCase):
    MODEL_OPTION = REAL_CONFIG_OPTIONS[0]
    EFFORT_OPTION = REAL_CONFIG_OPTIONS[1]

    def test_matches_a_bare_model_id_inside_the_json_pair(self):
        self.assertEqual(
            acp_bridge._match_config_value(self.MODEL_OPTION, 'deepseek-v4-pro'),
            '["deepseek-official","deepseek-v4-pro"]')

    def test_matches_the_display_name_case_insensitively(self):
        self.assertEqual(
            acp_bridge._match_config_value(self.MODEL_OPTION, 'deepseek-v4-flash'),
            '["deepseek-official","deepseek-v4-flash"]')
        self.assertEqual(
            acp_bridge._match_config_value(self.MODEL_OPTION, 'DeepSeek-V4-Pro'),
            '["deepseek-official","deepseek-v4-pro"]')

    def test_matches_the_exact_encoded_value(self):
        exact = '["deepseek-official","deepseek-v4-flash-vision-exp"]'
        self.assertEqual(
            acp_bridge._match_config_value(self.MODEL_OPTION, exact), exact)

    def test_flat_option_lists_work_too(self):
        self.assertEqual(
            acp_bridge._match_config_value(self.EFFORT_OPTION, 'low'), 'low')
        self.assertEqual(
            acp_bridge._match_config_value(self.EFFORT_OPTION, 'High'), 'high')

    def test_unknown_or_missing_returns_none(self):
        self.assertIsNone(
            acp_bridge._match_config_value(self.MODEL_OPTION, 'gpt-9'))
        self.assertIsNone(acp_bridge._match_config_value(None, 'x'))
        self.assertIsNone(
            acp_bridge._match_config_value(self.MODEL_OPTION, ''))


class PermissionOutcomeTest(unittest.TestCase):
    def test_prefers_allow_always_then_allow_once(self):
        self.assertEqual(
            acp_bridge.AcpBridge._permission_outcome({'options': [
                {'optionId': 'a', 'kind': 'allow_once'},
                {'optionId': 'b', 'kind': 'allow_always'}]}),
            {'outcome': 'selected', 'optionId': 'b'})
        self.assertEqual(
            acp_bridge.AcpBridge._permission_outcome({'options': [
                {'optionId': 'r', 'kind': 'reject_once'},
                {'optionId': 'a', 'kind': 'allow_once'}]}),
            {'outcome': 'selected', 'optionId': 'a'})

    def test_unknown_kind_is_treated_as_permissive(self):
        self.assertEqual(
            acp_bridge.AcpBridge._permission_outcome({'options': [
                {'optionId': 'x', 'kind': 'something_new'}]}),
            {'outcome': 'selected', 'optionId': 'x'})

    def test_reject_only_and_empty_option_sets_are_honest(self):
        self.assertEqual(
            acp_bridge.AcpBridge._permission_outcome({'options': [
                {'optionId': 'r', 'kind': 'reject_always'}]}),
            {'outcome': 'selected', 'optionId': 'r'})
        self.assertEqual(
            acp_bridge.AcpBridge._permission_outcome({'options': []}),
            {'outcome': 'cancelled'})
        self.assertEqual(acp_bridge.AcpBridge._permission_outcome({}),
                         {'outcome': 'cancelled'})


if __name__ == '__main__':
    unittest.main()
