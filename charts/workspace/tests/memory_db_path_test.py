"""The memory DB path is overridable, and its default is not (#599).

WHY THIS EXISTS. `/home/dev/.claude-memory/memory.db` is live shared state in
a workspace — the dashboard, the MCP server and every concurrent agent write
it. An agent doing schema work opened the live file read-write and left memory
half-broken workspace-wide (new memories saved, updates failed). There was no
supported way to point anything at a copy, so `$KC_MEMORY_DB` now exists.

Two failure shapes are guarded here, and the second is the dangerous one:

  1. The override does nothing / only some modules honour it. Then processes
     disagree about where memory lives, which is worse than no override.
  2. The DEFAULT drifts. Every deployed workspace's memories are already at
     the old path, so a typo in the constant silently hands every user an
     empty store — a far bigger outage than the bug being fixed. That is why
     `test_default_is_the_deployment_path` compares against a literal spelled
     out here rather than against the constant itself.

Nothing in this file opens the deployment path — the default is only ever
compared as a string (see tests/no_deployment_paths_test.py).

Run with:  python3 -m unittest tests.memory_db_path_test
(from charts/workspace/)
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(WORKSPACE))
sys.path.insert(0, WORKSPACE)

import memory.store as _store_mod  # noqa: E402

# The one true default, spelled out independently of the code under test.
# If you are changing this line, you are changing where every existing
# workspace looks for its memories. Don't.
DEPLOYMENT_DB = '/home/dev/.claude-memory/memory.db'

COPY_SCRIPT = os.path.join(REPO, 'scripts', 'memory-db-copy.sh')
CLAUDE_MD = os.path.join(WORKSPACE, 'claude-md.txt')


class ResolveTestCase(unittest.TestCase):
    """`_resolve_db_path` — pure, so it takes the env as an argument."""

    def test_default_is_the_deployment_path(self):
        self.assertEqual(_store_mod.DEFAULT_DB_PATH, DEPLOYMENT_DB)
        self.assertEqual(_store_mod._resolve_db_path({}), DEPLOYMENT_DB)

    def test_blank_env_is_an_unset_env(self):
        # An empty variable must never be read as "open '' as a database".
        for blank in ('', '   ', '\t\n'):
            with self.subTest(blank=repr(blank)):
                self.assertEqual(
                    _store_mod._resolve_db_path({'KC_MEMORY_DB': blank}),
                    DEPLOYMENT_DB,
                )

    def test_override_wins(self):
        self.assertEqual(
            _store_mod._resolve_db_path({'KC_MEMORY_DB': '/tmp/copy/memory.db'}),
            '/tmp/copy/memory.db',
        )

    def test_override_is_stripped(self):
        self.assertEqual(
            _store_mod._resolve_db_path({'KC_MEMORY_DB': '  /tmp/copy.db \n'}),
            '/tmp/copy.db',
        )


class ImportTimeTestCase(unittest.TestCase):
    """The module constant, as a freshly-started process actually sees it.

    Run out-of-process on purpose: DB_PATH is resolved once at import and
    every importer binds it with `from .store import DB_PATH`, so reloading
    the module in-process would prove nothing about them and would stomp on
    the rest of the suite's shared module state.
    """

    # Every module that ends up holding a copy of the constant, plus the
    # manager's real, live choice of file.
    _PROBE = (
        'import json, sys;'
        'sys.path.insert(0, %r);'
        'import memory, memory.store, memory.manager, memory.embeddings_worker;'
        'print(json.dumps({'
        '  "store": memory.store.DB_PATH,'
        '  "package": memory.DB_PATH,'
        '  "manager": memory.manager.DB_PATH,'
        '  "worker": memory.embeddings_worker.DB_PATH,'
        '  "default": memory.store.DEFAULT_DB_PATH,'
        '}))'
    )

    def _probe(self, kc_memory_db=None):
        env = dict(os.environ)
        env.pop('KC_MEMORY_DB', None)
        if kc_memory_db is not None:
            env['KC_MEMORY_DB'] = kc_memory_db
        out = subprocess.run(
            [sys.executable, '-c', self._PROBE % WORKSPACE],
            cwd=WORKSPACE, env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_unset_env_gives_every_module_the_deployment_default(self):
        seen = self._probe()
        self.assertEqual(seen.pop('default'), DEPLOYMENT_DB)
        for module, path in seen.items():
            with self.subTest(module=module):
                self.assertEqual(path, DEPLOYMENT_DB)

    def test_override_reaches_every_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = os.path.join(tmp, 'memory.db')
            seen = self._probe(copy)
            # The default constant stays put; only the resolved path moves.
            self.assertEqual(seen.pop('default'), DEPLOYMENT_DB)
            for module, path in seen.items():
                with self.subTest(module=module):
                    self.assertEqual(path, copy)

    def test_manager_opens_the_override_not_the_default(self):
        """The end-to-end claim: a process told to use a copy uses the copy.

        The child asserts where it landed BEFORE it writes anything. This is
        the one test here that mutates a database, so a regression in the
        resolver has to fail it — never quietly redirect the write onto the
        deployment path, which is the exact accident this issue is about.
        """
        with tempfile.TemporaryDirectory() as tmp:
            copy = os.path.join(tmp, 'memory.db')
            env = dict(os.environ)
            env['KC_MEMORY_DB'] = copy
            script = (
                'import sys; sys.path.insert(0, %r);'
                'import memory.store as s;'
                'expected = %r;'
                'assert s.DB_PATH == expected, '
                '  "resolved %%s — refusing to write" %% s.DB_PATH;'
                'from memory.manager import MemoryManager;'
                'MemoryManager.upsert(namespace="user.t", key="k", value="v");'
                'print(MemoryManager.store().db_path)'
            ) % (WORKSPACE, copy)
            out = subprocess.run(
                [sys.executable, '-c', script],
                cwd=WORKSPACE, env=env, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(out.stdout.strip(), copy)
            # The write landed in the copy, and the copy is a real store.
            self.assertTrue(os.path.exists(copy))
            conn = sqlite3.connect(copy)
            try:
                rows = conn.execute(
                    "SELECT value FROM memories WHERE key = 'k'"
                ).fetchall()
            finally:
                conn.close()
            self.assertEqual([r[0] for r in rows], ['v'])


class CopyHelperTestCase(unittest.TestCase):
    """scripts/memory-db-copy.sh — the alternative that makes the rule usable.

    A prohibition without a one-command alternative gets worked around, so the
    helper is treated as part of the fix and tested like code: it must produce
    a usable copy and must leave the source byte-for-byte identical.
    """

    def setUp(self):
        if not os.path.exists(COPY_SCRIPT):
            self.skipTest('helper not present in this checkout')
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.src = os.path.join(self._tmp.name, 'source.db')
        # A realistic source: the project's own schema, via the project's own
        # code — never a copy of anyone's live database.
        conn = sqlite3.connect(self.src)
        conn.close()
        _store_mod._INITIALIZED = False
        try:
            _store_mod.initialize(self.src)
        finally:
            _store_mod._INITIALIZED = False
        with sqlite3.connect(self.src) as c:
            c.execute(
                'INSERT INTO memories (namespace, key, value, kind, created_at,'
                ' updated_at) VALUES (?, ?, ?, ?, ?, ?)',
                ('user.t', 'k', 'v', 'semantic', 1.0, 1.0),
            )

    def _run(self, dest, src=None):
        env = dict(os.environ)
        env['KC_MEMORY_DB'] = self.src if src is None else src
        return subprocess.run(
            ['bash', COPY_SCRIPT, dest],
            env=env, capture_output=True, text=True, timeout=60,
        )

    def test_copy_is_usable_and_source_is_untouched(self):
        dest = os.path.join(self._tmp.name, 'copy.db')
        with open(self.src, 'rb') as fh:
            before = fh.read()

        out = self._run(dest)
        self.assertEqual(out.returncode, 0, out.stderr)

        # stdout is the path and nothing else, so `export KC_MEMORY_DB=$(...)`
        # works. Chatter belongs on stderr.
        self.assertEqual(out.stdout.strip(), dest)

        with open(self.src, 'rb') as fh:
            self.assertEqual(fh.read(), before, 'the helper mutated its source')

        conn = sqlite3.connect(dest)
        try:
            rows = conn.execute('SELECT value FROM memories').fetchall()
        finally:
            conn.close()
        self.assertEqual([r[0] for r in rows], ['v'])

    def test_missing_source_fails_loudly(self):
        out = self._run(
            os.path.join(self._tmp.name, 'copy.db'),
            src=os.path.join(self._tmp.name, 'nope.db'),
        )
        self.assertNotEqual(out.returncode, 0)
        self.assertIn('not found', out.stderr)

    def test_source_is_opened_read_only(self):
        """The property the whole issue turns on, asserted at the source."""
        with open(COPY_SCRIPT, encoding='utf-8') as fh:
            body = fh.read()
        self.assertIn('mode=ro', body)


class ShippedInstructionsTestCase(unittest.TestCase):
    """claude-md.txt is auto-loaded into every agent's context in every
    workspace, which makes it the highest-leverage place for this rule — and
    the easiest to quietly lose in a later edit."""

    def setUp(self):
        with open(CLAUDE_MD, encoding='utf-8') as fh:
            self.text = fh.read()

    def test_names_the_shared_stateful_paths(self):
        for path in (
            '/home/dev/.claude-memory/memory.db',
            '/home/dev/.claude-tasks/',
            '/home/dev/.credentials/',
        ):
            with self.subTest(path=path):
                self.assertIn(path, self.text)

    def test_states_the_copy_first_rule_and_the_way_to_do_it(self):
        self.assertIn('LIVE shared state', self.text)
        self.assertIn('KC_MEMORY_DB', self.text)
        self.assertIn('scripts/memory-db-copy.sh', self.text)


if __name__ == '__main__':
    unittest.main()
