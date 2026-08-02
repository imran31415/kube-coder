"""Unit tests for devcontainer.py — the pure half of devcontainer.json support (#594).

The JSONC parser is the load-bearing correctness risk in the feature: every
other behaviour depends on reading the file the same way Codespaces does. So
the comment/trailing-comma passes get the bulk of the coverage, including the
cases that break naive implementations (comment markers inside string values,
escaped quotes, a trailing backslash, an unterminated block comment).

The three denylists get equal weight for a different reason — they are the
security boundary, and they matter even in a deployment that never executes a
lifecycle command.

Run with:
    cd charts/workspace && python3 -m unittest tests.devcontainer_test
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import devcontainer as dc  # noqa: E402


class JsoncTests(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(dc.loads_jsonc('{"a": 1}'), {'a': 1})

    def test_line_comments_stripped(self):
        text = '{\n  // a comment\n  "a": 1 // trailing\n}'
        self.assertEqual(dc.loads_jsonc(text), {'a': 1})

    def test_block_comments_stripped(self):
        text = '{ /* hello\n   world */ "a": 1 }'
        self.assertEqual(dc.loads_jsonc(text), {'a': 1})

    def test_offsets_and_line_count_preserved(self):
        text = '{\n  // xxxx\n  /* y\n     z */\n  "a": 1\n}'
        cleaned = dc.strip_comments(text)
        self.assertEqual(len(cleaned), len(text))
        self.assertEqual(cleaned.count('\n'), text.count('\n'))

    def test_error_line_points_at_original_file(self):
        # The bad token is on line 4 of the ORIGINAL text; the comment above it
        # must not shift the reported position.
        text = '{\n  // a comment that is quite long\n  "a": 1,\n  "b": nope\n}'
        with self.assertRaises(dc.DevcontainerError) as ctx:
            dc.loads_jsonc(text)
        self.assertEqual(ctx.exception.line, 4)

    def test_comment_markers_inside_strings_untouched(self):
        text = '{"url": "https://example.com/x", "glob": "/* not a comment */"}'
        got = dc.loads_jsonc(text)
        self.assertEqual(got['url'], 'https://example.com/x')
        self.assertEqual(got['glob'], '/* not a comment */')

    def test_escaped_quote_inside_string(self):
        text = r'{"a": "he said \"// hi\"", "b": 2}'
        got = dc.loads_jsonc(text)
        self.assertEqual(got['a'], 'he said "// hi"')
        self.assertEqual(got['b'], 2)

    def test_escaped_backslash_then_quote_ends_string(self):
        # "a\\" is a string ending in one backslash; the // after it IS a comment.
        text = '{"a": "c:\\\\" // comment\n, "b": 2}'
        got = dc.loads_jsonc(text)
        self.assertEqual(got['a'], 'c:\\')
        self.assertEqual(got['b'], 2)

    def test_trailing_backslash_at_eof_terminates(self):
        with self.assertRaises(dc.DevcontainerError):
            dc.loads_jsonc('{"a": "unterminated \\')

    def test_trailing_commas_object_and_array(self):
        text = '{"a": [1, 2, ], "b": 3, }'
        self.assertEqual(dc.loads_jsonc(text), {'a': [1, 2], 'b': 3})

    def test_trailing_comma_with_comment_between(self):
        text = '{"a": [1, 2, // done\n]}'
        self.assertEqual(dc.loads_jsonc(text), {'a': [1, 2]})

    def test_comma_inside_string_not_stripped(self):
        self.assertEqual(dc.loads_jsonc('{"a": "x, ]"}'), {'a': 'x, ]'})

    def test_block_comments_do_not_nest(self):
        # The FIRST */ closes; the trailing */ is then a syntax error.
        with self.assertRaises(dc.DevcontainerError):
            dc.loads_jsonc('{ /* a /* b */ "a": 1 */ }')

    def test_unterminated_block_comment_raises_and_terminates(self):
        with self.assertRaises(dc.DevcontainerError) as ctx:
            dc.loads_jsonc('{ "a": 1 /* never closed')
        self.assertIn('unterminated', str(ctx.exception))

    def test_slash_slash_as_final_chars(self):
        self.assertEqual(dc.loads_jsonc('{"a": 1}//'), {'a': 1})

    def test_bom_and_crlf(self):
        text = '\ufeff{\r\n  // c\r\n  "a": 1\r\n}\r\n'
        self.assertEqual(dc.loads_jsonc(text), {'a': 1})

    def test_empty_file_raises(self):
        with self.assertRaises(dc.DevcontainerError):
            dc.loads_jsonc('')

    def test_non_object_top_level_raises(self):
        with self.assertRaises(dc.DevcontainerError) as ctx:
            dc.loads_jsonc('[1, 2]')
        self.assertIn('top level', str(ctx.exception))

    def test_lone_slash_is_a_syntax_error_not_a_hang(self):
        with self.assertRaises(dc.DevcontainerError):
            dc.loads_jsonc('{"a": / }')


class FindConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='kc-dc-find-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_none(self):
        self.assertEqual(dc.find_config(self.tmp), '')

    def test_folder_form_wins_over_dotfile(self):
        os.makedirs(os.path.join(self.tmp, '.devcontainer'))
        open(os.path.join(self.tmp, '.devcontainer', 'devcontainer.json'), 'w').close()
        open(os.path.join(self.tmp, '.devcontainer.json'), 'w').close()
        self.assertEqual(dc.find_config(self.tmp),
                         os.path.join(self.tmp, '.devcontainer', 'devcontainer.json'))

    def test_dotfile_form(self):
        open(os.path.join(self.tmp, '.devcontainer.json'), 'w').close()
        self.assertEqual(dc.find_config(self.tmp),
                         os.path.join(self.tmp, '.devcontainer.json'))

    def test_subfolder_form_deterministic(self):
        for name in ('zeta', 'alpha'):
            os.makedirs(os.path.join(self.tmp, '.devcontainer', name))
            open(os.path.join(self.tmp, '.devcontainer', name,
                              'devcontainer.json'), 'w').close()
        self.assertEqual(
            dc.find_config(self.tmp),
            os.path.join(self.tmp, '.devcontainer', 'alpha', 'devcontainer.json'))

    def test_oversize_refused_before_read(self):
        path = os.path.join(self.tmp, '.devcontainer.json')
        with open(path, 'w') as f:
            f.write('{"name": "' + ('x' * (dc.MAX_BYTES + 10)) + '"}')
        with self.assertRaises(dc.DevcontainerError) as ctx:
            dc.read_raw(path)
        self.assertIn('limit', str(ctx.exception))


class CommandTests(unittest.TestCase):
    WD = '/home/dev/api'

    def test_string_becomes_shell(self):
        cmds = dc.normalize_commands('npm install', self.WD)
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0]['kind'], 'shell')
        self.assertEqual(cmds[0]['command'], 'npm install')

    def test_array_becomes_argv(self):
        cmds = dc.normalize_commands(['npm', 'ci', '--no-audit'], self.WD)
        self.assertEqual(cmds[0]['kind'], 'argv')
        self.assertEqual(cmds[0]['command'], ['npm', 'ci', '--no-audit'])

    def test_object_runs_sequentially_in_key_order(self):
        cmds = dc.normalize_commands(
            {'install': 'npm i', 'build': ['make', 'all']}, self.WD)
        self.assertEqual([c['name'] for c in cmds], ['install', 'build'])
        self.assertEqual(cmds[1]['kind'], 'argv')
        self.assertTrue(any('sequentially' in c for c in cmds[0]['caveats']))

    def test_empty_and_missing(self):
        self.assertEqual(dc.normalize_commands(None, self.WD), [])
        self.assertEqual(dc.normalize_commands('   ', self.WD), [])

    def test_bad_type_raises(self):
        with self.assertRaises(dc.DevcontainerError):
            dc.normalize_commands(42, self.WD)
        with self.assertRaises(dc.DevcontainerError):
            dc.normalize_commands([{'a': 1}], self.WD)

    def test_sudo_and_apt_flagged_needs_root(self):
        for text in ('sudo apt-get install -y jq', 'apk add curl',
                     'docker compose up -d'):
            cmds = dc.normalize_commands(text, self.WD)
            self.assertTrue(cmds[0]['needs_root'], text)
            self.assertTrue(cmds[0]['root_reasons'], text)

    def test_ordinary_command_not_flagged(self):
        cmds = dc.normalize_commands('npm ci && pip install --user -r req.txt',
                                     self.WD)
        self.assertFalse(cmds[0]['needs_root'])

    def test_workspace_folder_variable_substituted(self):
        cmds = dc.normalize_commands('cd ${workspaceFolder} && ls', self.WD)
        self.assertEqual(cmds[0]['command'], 'cd /home/dev/api && ls')

    def test_local_env_variable_not_substituted(self):
        cmds = dc.normalize_commands('echo ${localEnv:ANTHROPIC_API_KEY}', self.WD)
        self.assertIn('${localEnv:ANTHROPIC_API_KEY}', cmds[0]['command'])
        self.assertTrue(any('unresolved' in c for c in cmds[0]['caveats']))

    def test_hook_hash_stable_and_discriminating(self):
        a = dc.hook_hash(dc.normalize_commands('npm ci', self.WD))
        b = dc.hook_hash(dc.normalize_commands('npm ci', self.WD))
        c = dc.hook_hash(dc.normalize_commands('npm ci --force', self.WD))
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)


class PortTests(unittest.TestCase):
    def test_labels_from_ports_attributes(self):
        ok, skipped = dc.normalize_ports(
            [3000, '5173'], {'3000': {'label': 'API'}})
        self.assertEqual(ok[0], {'port': 3000, 'label': 'API'})
        self.assertEqual(ok[1]['label'], 'port 5173')
        self.assertEqual(skipped, [])

    def test_internal_ports_skipped_with_reason(self):
        ok, skipped = dc.normalize_ports([8080, 7681, 3000], None)
        self.assertEqual([p['port'] for p in ok], [3000])
        self.assertEqual(len(skipped), 2)
        self.assertIn('reserved', skipped[0]['reason'])

    def test_host_port_form_rejected(self):
        ok, skipped = dc.normalize_ports(['db:5432'], None)
        self.assertEqual(ok, [])
        self.assertIn('single pod', skipped[0]['reason'])

    def test_out_of_range_and_junk(self):
        ok, skipped = dc.normalize_ports([0, 70000, 'x', True, None], None)
        self.assertEqual(ok, [])
        self.assertEqual(len(skipped), 5)

    def test_duplicates_collapse(self):
        ok, _ = dc.normalize_ports([3000, 3000, '3000'], None)
        self.assertEqual(len(ok), 1)


class ExtensionTests(unittest.TestCase):
    def test_valid_ids_accepted(self):
        ok, bad = dc.normalize_extensions(
            ['ms-python.python', 'dbaeumer.vscode-eslint', 'foo.bar@1.2.3'])
        self.assertEqual(len(ok), 3)
        self.assertEqual(bad, [])

    def test_leading_dash_rejected(self):
        # Would be read as a flag by code-server --install-extension.
        ok, bad = dc.normalize_extensions(['--force'])
        self.assertEqual(ok, [])
        self.assertTrue(bad)

    def test_paths_and_vsix_rejected(self):
        ok, bad = dc.normalize_extensions(
            ['./evil.vsix', '../../tmp/x.vsix', '/abs/path.vsix',
             'sub/dir.thing'])
        self.assertEqual(ok, [])
        self.assertEqual(len(bad), 4)
        self.assertIn('file path', bad[0]['reason'])

    def test_non_strings_and_dupes(self):
        ok, bad = dc.normalize_extensions(['a.b', 'A.B', 5, ''])
        self.assertEqual(ok, ['a.b'])
        self.assertEqual(len(bad), 1)


class SettingTests(unittest.TestCase):
    def test_ordinary_settings_accepted(self):
        ok, denied = dc.normalize_settings(
            {'editor.formatOnSave': True, 'files.eol': '\n'})
        self.assertEqual(len(ok), 2)
        self.assertEqual(denied, [])

    def test_terminal_profiles_denied(self):
        ok, denied = dc.normalize_settings({
            'terminal.integrated.profiles.linux': {'evil': {'path': '/bin/sh'}},
            'terminal.integrated.defaultProfile.linux': 'evil',
            'terminal.integrated.automationProfile.linux': {},
            'terminal.integrated.env.linux': {'X': '1'},
        })
        self.assertEqual(ok, {})
        self.assertEqual(len(denied), 4)

    def test_executable_path_settings_denied(self):
        ok, denied = dc.normalize_settings({
            'python.defaultInterpreterPath': './venv/bin/python',
            'go.alternateTools.serverPath': '/x',
            'rust-analyzer.server.extraEnv.shellArgs': 'x',
            'git.path': '/tmp/git',
        })
        self.assertEqual(ok, {})
        self.assertEqual(len(denied), 4)

    def test_workspace_trust_and_remote_denied(self):
        ok, denied = dc.normalize_settings({
            'security.workspace.trust.enabled': False,
            'remote.SSH.path': '/x',
            'http.proxy': 'http://evil',
        })
        self.assertEqual(ok, {})
        self.assertEqual(len(denied), 3)

    def test_invalid_key_and_huge_value(self):
        ok, denied = dc.normalize_settings({
            'bad key!': 1, 'editor.x': 'y' * (9000)})
        self.assertEqual(ok, {})
        self.assertEqual(len(denied), 2)


class EnvTests(unittest.TestCase):
    def test_ordinary_env_accepted(self):
        ok, denied = dc.normalize_env({'NODE_ENV': 'development', 'PORT': '3000'})
        self.assertEqual(ok, {'NODE_ENV': 'development', 'PORT': '3000'})
        self.assertEqual(denied, [])

    def test_provider_prefixes_denied(self):
        ok, denied = dc.normalize_env({
            'ANTHROPIC_BASE_URL': 'https://evil.example',
            'OPENAI_API_KEY': 'x', 'GH_TOKEN': 'x', 'GITHUB_TOKEN': 'x',
            'KC_ANYTHING': 'x', 'CLAUDE_CODE_X': 'x',
        })
        self.assertEqual(ok, {})
        self.assertEqual(len(denied), 6)

    def test_shell_hijack_vars_denied(self):
        ok, denied = dc.normalize_env({
            'PATH': '/evil:$PATH', 'LD_PRELOAD': '/tmp/x.so',
            'BASH_ENV': '/tmp/rc', 'GIT_SSH_COMMAND': 'sh -c evil',
            'NODE_OPTIONS': '--require /tmp/x', 'HOME': '/tmp',
        })
        self.assertEqual(ok, {})
        self.assertEqual(len(denied), 6)

    def test_bad_names_and_values(self):
        ok, denied = dc.normalize_env({
            '1BAD': 'x', 'GOOD': 'a\nb', 'ALSO': {'not': 'a string'},
            'BOOLY': True})
        self.assertEqual(ok, {})
        self.assertEqual(len(denied), 4)

    def test_variables_substituted_in_values(self):
        ok, _ = dc.normalize_env({'ROOT': '${workspaceFolder}/src'},
                                 '/home/dev/api')
        self.assertEqual(ok['ROOT'], '/home/dev/api/src')


class WorkspaceFolderTests(unittest.TestCase):
    WD = '/home/dev/api'

    def test_absent_maps_to_workdir(self):
        self.assertEqual(dc.map_workspace_folder(self.WD, None), (self.WD, ''))

    def test_canonical_container_path_maps(self):
        self.assertEqual(
            dc.map_workspace_folder(self.WD, '/workspaces/api'), (self.WD, ''))

    def test_subpath_maps(self):
        got, caveat = dc.map_workspace_folder(self.WD, '/workspaces/api/server')
        # normpath: the function returns a NATIVE path, which only differs from
        # the POSIX form when the suite runs on a developer's Windows box.
        self.assertEqual(got, os.path.normpath('/home/dev/api/server'))
        self.assertEqual(caveat, '')

    def test_foreign_absolute_path_refused_with_caveat(self):
        got, caveat = dc.map_workspace_folder(self.WD, '/etc')
        self.assertEqual(got, self.WD)
        self.assertIn('does not map', caveat)

    def test_traversal_refused(self):
        got, caveat = dc.map_workspace_folder(self.WD, '../../etc')
        self.assertEqual(got, self.WD)
        self.assertIn('escapes', caveat)

    def test_non_string_refused(self):
        got, caveat = dc.map_workspace_folder(self.WD, 42)
        self.assertEqual(got, self.WD)
        self.assertTrue(caveat)


class ClassifyTests(unittest.TestCase):
    def test_features_is_blocking_with_reason_and_remedy(self):
        out = dc.classify_unsupported(
            {'features': {'ghcr.io/devcontainers/features/node:1': {}}})
        self.assertEqual(len(out), 1)
        entry = out[0]
        self.assertEqual(entry['severity'], 'blocking')
        self.assertIn('UID 1000', entry['reason'])
        self.assertIn('postCreateCommand', entry['remedy'])
        self.assertIn('node', entry['detail'])

    def test_image_build_compose_blocking(self):
        out = dc.classify_unsupported({
            'image': 'node:20', 'build': {'dockerfile': 'Dockerfile'},
            'dockerComposeFile': 'compose.yml'})
        self.assertEqual({e['key'] for e in out},
                         {'image', 'build', 'dockerComposeFile'})
        self.assertTrue(all(e['severity'] == 'blocking' for e in out))

    def test_initialize_and_post_attach_ignored(self):
        out = dc.classify_unsupported({
            'initializeCommand': 'echo hi', 'postAttachCommand': 'echo bye'})
        self.assertTrue(all(e['severity'] == 'ignored' for e in out))

    def test_empty_values_not_reported(self):
        self.assertEqual(dc.classify_unsupported(
            {'features': {}, 'mounts': [], 'image': ''}), [])

    def test_supported_keys_never_reported(self):
        out = dc.classify_unsupported({
            'name': 'x', 'forwardPorts': [3000], 'postCreateCommand': 'npm i',
            'customizations': {'vscode': {'extensions': []}}})
        self.assertEqual(out, [])

    def test_other_tool_customizations_reported(self):
        out = dc.classify_unsupported({'customizations': {'jetbrains': {}}})
        self.assertEqual(out[0]['key'], 'customizations.jetbrains')

    def test_blocking_sorts_first(self):
        out = dc.classify_unsupported({
            'initializeCommand': 'x', 'image': 'y', 'mounts': ['z']})
        self.assertEqual([e['severity'] for e in out],
                         ['blocking', 'partial', 'ignored'])


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='kc-dc-parse-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _write(self, obj_or_text):
        os.makedirs(os.path.join(self.tmp, '.devcontainer'), exist_ok=True)
        path = os.path.join(self.tmp, '.devcontainer', 'devcontainer.json')
        text = obj_or_text if isinstance(obj_or_text, str) else json.dumps(obj_or_text)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        return path

    def test_absent(self):
        rec = dc.parse(self.tmp)
        self.assertFalse(rec['found'])
        self.assertEqual(rec['error'], '')

    def test_full_record(self):
        self._write({
            'name': 'Node 20 + Postgres',
            'forwardPorts': [3000, 8080],
            'portsAttributes': {'3000': {'label': 'API'}},
            'postCreateCommand': 'npm ci',
            'postStartCommand': ['npm', 'start'],
            'containerEnv': {'NODE_ENV': 'development', 'PATH': '/evil'},
            'customizations': {'vscode': {
                'extensions': ['ms-python.python', './evil.vsix'],
                'settings': {'editor.formatOnSave': True,
                             'terminal.integrated.profiles.linux': {}},
            }},
            'features': {'ghcr.io/devcontainers/features/node:1': {}},
        })
        rec = dc.parse(self.tmp)
        self.assertTrue(rec['found'])
        self.assertEqual(rec['error'], '')
        self.assertEqual(rec['name'], 'Node 20 + Postgres')
        self.assertEqual([p['port'] for p in rec['ports']], [3000])
        self.assertEqual(len(rec['ports_skipped']), 1)
        self.assertEqual(rec['extensions'], ['ms-python.python'])
        self.assertEqual(len(rec['extensions_rejected']), 1)
        self.assertEqual(rec['settings'], {'editor.formatOnSave': True})
        self.assertEqual(len(rec['settings_denied']), 1)
        self.assertEqual(rec['env'], {'NODE_ENV': 'development'})
        self.assertEqual(len(rec['env_denied']), 1)
        self.assertEqual(len(rec['lifecycle']['postCreate']), 1)
        self.assertEqual(len(rec['lifecycle']['postStart']), 1)
        self.assertEqual(rec['lifecycle']['onCreate'], [])
        self.assertTrue(any(u['key'] == 'features' for u in rec['unsupported']))
        self.assertEqual(len(rec['config_hash']), 64)

    def test_unparseable_reports_error_not_raise(self):
        self._write('{ "a": nope }')
        rec = dc.parse(self.tmp)
        self.assertTrue(rec['found'])
        self.assertIn('invalid JSON', rec['error'])
        self.assertEqual(rec['error_line'], 1)

    def test_needs_root_surfaces_as_unsupported(self):
        self._write({'postCreateCommand': 'sudo apt-get install -y jq'})
        rec = dc.parse(self.tmp)
        self.assertTrue(rec['needs_root'])
        self.assertTrue(any('root' in u['key'] for u in rec['unsupported']))

    def test_per_hook_hash_isolation(self):
        """Editing forwardPorts must NOT stale a successful postCreate."""
        self._write({'postCreateCommand': 'npm ci', 'forwardPorts': [3000]})
        first = dc.parse(self.tmp)
        self._write({'postCreateCommand': 'npm ci', 'forwardPorts': [3000, 4000]})
        second = dc.parse(self.tmp)
        self.assertNotEqual(first['config_hash'], second['config_hash'])
        self.assertEqual(first['hook_hashes']['postCreate'],
                         second['hook_hashes']['postCreate'])

    def test_commands_run_in_mapped_workspace_folder(self):
        self._write({'workspaceFolder': '/workspaces/{}/srv'.format(
            os.path.basename(self.tmp)), 'postCreateCommand': 'echo ${workspaceFolder}'})
        rec = dc.parse(self.tmp)
        self.assertEqual(rec['workspace_folder'], os.path.join(self.tmp, 'srv'))
        self.assertIn(os.path.join(self.tmp, 'srv'),
                      rec['lifecycle']['postCreate'][0]['command'])

    def test_summarize_is_counts_only(self):
        self._write({'name': 'X', 'forwardPorts': [3000],
                     'postCreateCommand': 'npm ci',
                     'features': {'a': {}}})
        s = dc.summarize(self.tmp)
        self.assertEqual(s['ports'], 1)
        self.assertEqual(s['hooks'], {'postCreate': 1})
        self.assertEqual(s['blocking'], 1)
        self.assertEqual(s['blocking_keys'], ['features'])

    def test_summarize_absent_and_broken(self):
        self.assertEqual(dc.summarize(self.tmp), {'found': False})
        self._write('{ oops')
        s = dc.summarize(self.tmp)
        self.assertTrue(s['found'])
        self.assertTrue(s['error'])


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='kc-dc-state-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._state_dir = dc.STATE_DIR
        self._boot = dc.BOOT_MARKER
        dc.STATE_DIR = os.path.join(self.tmp, '.claude-devcontainer')
        dc.BOOT_MARKER = os.path.join(self.tmp, 'boot')

    def tearDown(self):
        dc.STATE_DIR = self._state_dir
        dc.BOOT_MARKER = self._boot

    def test_roundtrip_atomic_and_0600(self):
        dc.put_record('/home/dev/api', {'config_hash': 'abc'})
        self.assertEqual(dc.get_record('/home/dev/api')['config_hash'], 'abc')
        if os.name == 'posix':   # Windows chmod only honours the read-only bit
            mode = os.stat(dc.state_path()).st_mode & 0o777
            self.assertEqual(mode, 0o600)
        self.assertEqual(
            [n for n in os.listdir(dc.STATE_DIR) if n.startswith('state.json.tmp')],
            [])

    def test_missing_and_corrupt_state_degrade_to_empty(self):
        self.assertEqual(dc.read_state()['workdirs'], {})
        dc.ensure_dirs()
        with open(dc.state_path(), 'w') as f:
            f.write('not json')
        self.assertEqual(dc.read_state()['workdirs'], {})

    def test_drop_record(self):
        dc.put_record('/a', {'x': 1})
        self.assertTrue(dc.drop_record('/a'))
        self.assertFalse(dc.drop_record('/a'))

    def test_boot_id_is_stable_within_a_boot(self):
        first = dc.boot_id()
        self.assertEqual(first, dc.boot_id())
        os.remove(dc.BOOT_MARKER)          # simulates a pod restart wiping /tmp
        self.assertNotEqual(first, dc.boot_id())

    def _parsed(self, **hooks):
        life = {h: [] for h in dc.HOOKS}
        life.update(hooks)
        return {'lifecycle': life,
                'hook_hashes': {h: dc.hook_hash(life[h]) for h in dc.HOOKS}}

    def test_lifecycle_status_none_and_pending(self):
        cmds = [{'name': '', 'kind': 'shell', 'command': 'npm ci'}]
        st = dc.lifecycle_status(self._parsed(postCreate=cmds), {})
        self.assertEqual(st['onCreate']['status'], 'none')
        self.assertEqual(st['postCreate']['status'], 'pending')

    def test_lifecycle_status_done_stale_failed(self):
        cmds = [{'name': '', 'kind': 'shell', 'command': 'npm ci'}]
        parsed = self._parsed(postCreate=cmds)
        h = parsed['hook_hashes']['postCreate']
        done = {'lifecycle': {'postCreate': {'hook_hash': h, 'status': 'ok'}}}
        self.assertEqual(
            dc.lifecycle_status(parsed, done)['postCreate']['status'], 'done')
        stale = {'lifecycle': {'postCreate': {'hook_hash': 'other', 'status': 'ok'}}}
        self.assertEqual(
            dc.lifecycle_status(parsed, stale)['postCreate']['status'], 'stale')
        failed = {'lifecycle': {'postCreate': {'hook_hash': h, 'status': 'failed',
                                               'exit_code': 100}}}
        got = dc.lifecycle_status(parsed, failed)['postCreate']
        self.assertEqual(got['status'], 'failed')
        self.assertEqual(got['exit_code'], 100)

    def test_post_start_is_pending_on_a_new_boot(self):
        cmds = [{'name': '', 'kind': 'shell', 'command': 'npm start'}]
        parsed = self._parsed(postStart=cmds)
        h = parsed['hook_hashes']['postStart']
        rec = {'lifecycle': {'postStart': {'hook_hash': h, 'status': 'ok',
                                           'boot_id': 'old-boot'}}}
        self.assertEqual(
            dc.lifecycle_status(parsed, rec, current_boot='new-boot')['postStart']
            ['status'], 'pending')
        self.assertEqual(
            dc.lifecycle_status(parsed, rec, current_boot='old-boot')['postStart']
            ['status'], 'done')

    def test_log_path_is_slugified_and_contained(self):
        dc.ensure_dirs()
        p = dc.log_path_for('/home/dev/../../etc/api', 'postCreate', when=1)
        self.assertTrue(p.startswith(dc.log_dir() + os.sep))
        self.assertNotIn('..', os.path.basename(p))


if __name__ == '__main__':
    unittest.main()
