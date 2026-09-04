"""Sub-agent orchestration + skills sync for the DeepSeek Harness (#639).

Covers the two remaining acceptance criteria: `deepseek-harness` is
selectable from the agent orchestrator and accepted by its MCP tool enum,
and the skills syncer targets it like any other provider.

Run with:  python3 -m unittest tests.dsh_orchestrator_test  (from charts/workspace/)
"""

import os
import shlex
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import mcp_agent_orchestrator as orch  # noqa: E402
import mcp_registry  # noqa: E402
import server  # noqa: E402
from skills.providers import PROVIDERS  # noqa: E402
from skills.providers.deepseek_harness import DeepseekHarnessProvider  # noqa: E402

DSH = 'deepseek-harness'


class OrchestratorRegistrationTest(unittest.TestCase):
    def test_headless_capable(self):
        # The bridge's default mode takes one prompt and exits, which is
        # exactly the contract this module needs to detect completion by
        # session death + exit code.
        self.assertIn(DSH, orch._HEADLESS_CAPABLE)

    def test_listed_and_accepted_by_the_tool_enum(self):
        self.assertIn(DSH, [a['id'] for a in orch._ASSISTANTS_LIST])
        enum = orch.TOOLS['spawn_agent']['schema']['inputSchema'][
            'properties']['assistant']['enum']
        self.assertIn(DSH, enum)

    def test_the_enum_covers_every_listed_assistant(self):
        enum = set(orch.TOOLS['spawn_agent']['schema']['inputSchema'][
            'properties']['assistant']['enum'])
        self.assertEqual({a['id'] for a in orch._ASSISTANTS_LIST}, enum)

    def test_ids_match_the_server_registry(self):
        # A drifting id would let a spawn request through here and then fail
        # to resolve an assistant on the other side.
        self.assertIn(DSH, server.ClaudeTaskManager.ASSISTANTS)
        for entry in orch._ASSISTANTS_LIST:
            with self.subTest(assistant=entry['id']):
                self.assertIn(entry['id'], server.ClaudeTaskManager.ASSISTANTS)


class OrchestratorCommandTest(unittest.TestCase):
    def cmd(self, prompt='do the thing', headless=True, env=None):
        with mock.patch.dict(os.environ, env or {}, clear=True):
            return orch._assistant_command(DSH, prompt, headless=headless)

    def test_headless_runs_the_bridge_one_shot(self):
        cmd = self.cmd()
        self.assertIn('python3 /tmp/browser/acp_bridge.py', cmd)
        self.assertNotIn('--serve', cmd)
        self.assertIn('--format stream-json', cmd)

    def test_headless_feeds_the_prompt_on_stdin_not_argv(self):
        # Keeps an arbitrary prompt out of `ps`, and out of reach of the
        # shell that builds this command line.
        cmd = self.cmd(prompt="rm -rf /; echo $(whoami)")
        head, _, tail = cmd.partition('|')
        argv = shlex.split(head)
        self.assertEqual(argv[0], 'printf')
        self.assertEqual(argv[1], '%s')
        self.assertEqual(argv[2], 'rm -rf /; echo $(whoami)')
        self.assertNotIn('rm', shlex.split(tail))

    def test_a_percent_in_the_prompt_is_inert(self):
        # printf's format string is a literal '%s', so a prompt containing a
        # format specifier is data, not a directive.
        cmd = self.cmd(prompt='what does %s mean in printf?')
        argv = shlex.split(cmd.partition('|')[0])
        self.assertEqual(argv[1], '%s')
        self.assertEqual(argv[2], 'what does %s mean in printf?')

    def test_interactive_uses_serve_mode(self):
        cmd = self.cmd(headless=False)
        self.assertIn('--serve', cmd)
        self.assertNotIn('printf', cmd)

    def test_model_comes_from_the_pod_default(self):
        cmd = self.cmd(env={'KC_DSH_MODEL': 'deepseek-v4-pro'})
        argv = shlex.split(cmd.partition('|')[2])
        self.assertEqual(argv[argv.index('--model') + 1], 'deepseek-v4-pro')

    def test_a_hostile_model_var_cannot_inject(self):
        cmd = self.cmd(env={'KC_DSH_MODEL': "x'; touch /tmp/pwned; #"})
        argv = shlex.split(cmd.partition('|')[2])
        self.assertEqual(argv[argv.index('--model') + 1], "x'; touch /tmp/pwned; #")
        self.assertNotIn('touch', argv)

    def test_no_model_flag_when_unset(self):
        self.assertNotIn('--model', self.cmd())

    def test_cwd_is_the_subagents_workdir(self):
        # spawn_agent wraps this in `cd <workdir> && …` under `bash -lc`.
        self.assertIn('--cwd "$PWD"', self.cmd())

    def test_other_assistants_are_untouched(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(orch._assistant_command('claude', 'hi'),
                             "claude --dangerously-skip-permissions -p hi")
            self.assertEqual(orch._assistant_command('ante', 'hi'),
                             'ante --yolo -p hi')
            self.assertEqual(orch._assistant_command('kc-harness', 'hi',
                                                     headless=False),
                             'python3 /tmp/browser/harness.py')


class SkillsProviderTest(unittest.TestCase):
    def setUp(self):
        self.p = DeepseekHarnessProvider()
        self.tmp = tempfile.mkdtemp(prefix='dsh-skills-')

    def test_registered_under_the_assistant_id(self):
        self.assertIn(DSH, PROVIDERS)
        self.assertIs(type(PROVIDERS[DSH]), DeepseekHarnessProvider)
        self.assertEqual(PROVIDERS[DSH].key, DSH)
        self.assertIn(DSH, server.ClaudeTaskManager.ASSISTANTS)

    def user_roots(self, env):
        with mock.patch.dict(os.environ, env, clear=True):
            return [p for scope, p in self.p.scan_roots() if scope == 'user']

    def test_scans_the_documented_user_roots(self):
        roots = self.user_roots({})
        self.assertIn('/home/dev/.dsh/skills', roots)
        self.assertIn('/home/dev/.agents/skills', roots)

    def test_dsh_home_env_overrides_the_user_root(self):
        roots = self.user_roots({'DSH_HOME': '/mnt/dsh'})
        self.assertIn('/mnt/dsh/skills', roots)
        self.assertNotIn('/home/dev/.dsh/skills', roots)

    def test_agents_home_env_overrides_the_shared_root(self):
        roots = self.user_roots({'DSH_AGENTS_HOME': '/mnt/agents'})
        self.assertIn('/mnt/agents/skills', roots)
        self.assertNotIn('/home/dev/.agents/skills', roots)

    def test_a_blank_dsh_home_counts_as_unset(self):
        # The harness's own rule — otherwise a stray empty var resolves the
        # root to the current working directory.
        self.assertIn('/home/dev/.dsh/skills', self.user_roots({'DSH_HOME': '   '}))

    def test_project_roots_include_both_conventions(self):
        with mock.patch.dict(os.environ,
                             {'KC_SKILLS_PROJECT_ROOTS': self.tmp}, clear=True):
            os.makedirs(os.path.join(self.tmp, 'repo'), exist_ok=True)
            paths = [p for s, p in self.p.scan_roots() if s == 'project']
        self.assertIn(os.path.join(self.tmp, '.dsh', 'skills'), paths)
        self.assertIn(os.path.join(self.tmp, '.agents', 'skills'), paths)
        self.assertIn(os.path.join(self.tmp, 'repo', '.dsh', 'skills'), paths)

    # ── the two layout divergences ──────────────────────────────────────

    def _write(self, rel, body):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(body)
        return path

    FRONT = '---\nname: {name}\ndescription: {desc}\n---\n\n{body}\n'

    def test_discovers_both_the_bundle_and_the_flat_form(self):
        self._write('bundled/SKILL.md',
                    self.FRONT.format(name='bundled', desc='a bundle', body='B'))
        self._write('flat.md',
                    self.FRONT.format(name='flat', desc='a flat file', body='F'))
        found = sorted(os.path.basename(p)
                       for p in self.p._iter_skill_files(self.tmp))
        self.assertEqual(found, ['SKILL.md', 'flat.md'])

    def test_does_not_recurse(self):
        # The harness explicitly does not discover nested **/SKILL.md.
        self._write('deep/nested/SKILL.md',
                    self.FRONT.format(name='deep', desc='d', body='D'))
        self._write('deep/notes.md', '# just a note\n')
        self.assertEqual(list(self.p._iter_skill_files(self.tmp)), [])

    def test_skips_the_reserved_system_child(self):
        # $DSH_HOME/skills/.system holds the harness's own built-ins.
        self._write('.system/builtin/SKILL.md',
                    self.FRONT.format(name='builtin', desc='b', body='B'))
        self._write('mine/SKILL.md',
                    self.FRONT.format(name='mine', desc='m', body='M'))
        found = [p for p in self.p._iter_skill_files(self.tmp)]
        self.assertEqual(len(found), 1)
        self.assertIn('mine', found[0])

    def test_a_flat_skill_takes_its_name_from_the_file_stem(self):
        # The parent directory is the ROOT, not the skill, so the usual
        # folder-name fallback would be wrong here.
        self._write('review-pr.md', '# no frontmatter at all\n')
        rec = self.p._load_one(os.path.join(self.tmp, 'review-pr.md'), 'user')
        self.assertIsNotNone(rec)
        self.assertEqual(rec.name, 'review-pr')
        self.assertEqual(rec.systems, [DSH])

    def test_a_bundle_still_takes_its_name_from_the_folder(self):
        # The shared fallback had to learn the flat form without changing this.
        self._write('deploy-app/SKILL.md', '# no frontmatter at all\n')
        rec = self.p._load_one(
            os.path.join(self.tmp, 'deploy-app', 'SKILL.md'), 'user')
        self.assertEqual(rec.name, 'deploy-app')

    # ── write path ──────────────────────────────────────────────────────

    def test_writes_bundles_into_dot_dsh_only(self):
        with mock.patch.dict(os.environ, {'DSH_HOME': self.tmp}, clear=True):
            self.assertTrue(self.p.writable())
            self.assertEqual(self.p.install_path('demo', 'user'),
                             os.path.join(self.tmp, 'skills', 'demo', 'SKILL.md'))

    def test_project_installs_go_to_dot_dsh_not_dot_agents(self):
        # `.agents/skills` is a shared cross-harness root: we read it, we do
        # not own it.
        with mock.patch.dict(os.environ,
                             {'KC_SKILLS_PROJECT_ROOTS': self.tmp}, clear=True):
            dest = self.p.install_path('demo', 'project')
        self.assertEqual(dest,
                         os.path.join(self.tmp, '.dsh', 'skills', 'demo',
                                      'SKILL.md'))
        self.assertNotIn('.agents', dest)

    def test_unsafe_names_are_refused(self):
        with mock.patch.dict(os.environ, {'DSH_HOME': self.tmp}, clear=True):
            with self.assertRaises(ValueError):
                self.p.install_path('../../etc/passwd', 'user')

    def test_install_round_trips(self):
        from skills.model import SkillRecord
        rec = SkillRecord(name='demo', description='a demo',
                          body='Do the thing.', scope='user', systems=[DSH])
        with mock.patch.dict(os.environ, {'DSH_HOME': self.tmp}, clear=True):
            path = self.p.install(rec, 'user')
            self.assertTrue(os.path.isfile(path))
            back = self.p._load_one(path, 'user')
        self.assertEqual(back.name, 'demo')
        self.assertEqual(back.description, 'a demo')
        self.assertIn('Do the thing.', back.body)

    def test_disabling_it_stops_every_scan(self):
        with mock.patch.object(DeepseekHarnessProvider, 'enabled', False):
            p = DeepseekHarnessProvider()
            self.assertEqual(p.scan(), [])
            self.assertEqual(p.roots_mtime_fingerprint(), {})
            self.assertFalse(p.writable())


class McpRegistryTest(unittest.TestCase):
    def test_the_harness_is_deliberately_not_a_file_sync_provider(self):
        # Its MCP surface is per-session over ACP (`session/new` takes the
        # server list), so there is no config file to merge into. A no-op
        # entry would make sync_all() report a provider it never syncs.
        self.assertNotIn(DSH, mcp_registry._PROVIDERS)
        for provider in mcp_registry._PROVIDERS:
            with self.subTest(provider=provider):
                self.assertTrue(hasattr(mcp_registry, f'_sync_{provider}'))


if __name__ == '__main__':
    unittest.main()
