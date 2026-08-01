"""Namespace-scoped memory retrieval (#359).

Injection used to retrieve from the whole workspace DB, so a `project.foo`
memory could surface inside a `project.bar` chat whenever it out-scored the
local ones. These tests pin the scoping contract, including the prefix-anchoring
and LIKE-escaping edge cases that make `project.foo` NOT match `project.foobar`
or `project.foo_x`.

Run with:   python3 -m unittest tests.memory_scope_test   (from charts/workspace/)
"""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from memory import store as _store_mod  # noqa: E402
from memory.store import MemoryStore  # noqa: E402
from memory.manager import MemoryManager  # noqa: E402


class ScopeTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._orig_store = MemoryManager._store
        self._orig_init = _store_mod._INITIALIZED
        _store_mod._INITIALIZED = False
        MemoryManager._store = MemoryStore(
            os.path.join(self._tmpdir.name, 'memory.db'))
        # A deliberately adversarial namespace set: siblings that share a
        # textual prefix with the scope, and one containing a LIKE wildcard.
        for ns, key in (
            ('project.foo', 'a'),
            ('project.foo.decisions', 'b'),
            ('project.foo.goals', 'c'),
            ('project.foobar', 'd'),      # prefix-shares, must NOT match
            ('project.foo_x', 'e'),       # '_' is a LIKE wildcard, must NOT match
            ('project.bar', 'f'),
            ('user', 'g'),
        ):
            MemoryManager.upsert(namespace=ns, key=key,
                                 value='deployment pipeline notes')

    def tearDown(self):
        MemoryManager._store = self._orig_store
        _store_mod._INITIALIZED = self._orig_init

    def _namespaces(self, rows):
        return sorted(r['namespace'] for r in rows)

    IN_SCOPE = ['project.foo', 'project.foo.decisions', 'project.foo.goals']


class SearchScopeTests(ScopeTestCase):
    def test_scope_matches_root_and_descendants_only(self):
        rows = MemoryManager.search(q='deployment', namespace_scope='project.foo',
                                    limit=50)
        self.assertEqual(self._namespaces(rows), self.IN_SCOPE)

    def test_scope_excludes_prefix_sharing_sibling(self):
        # The classic bug: a naive LIKE 'project.foo%' also matches
        # `project.foobar`. Anchoring on the '.' separator prevents it.
        rows = MemoryManager.search(q='deployment', namespace_scope='project.foo',
                                    limit=50)
        self.assertNotIn('project.foobar', self._namespaces(rows))

    def test_scope_escapes_like_wildcards(self):
        # '_' matches any single char in LIKE; unescaped, `project.foo_x` would
        # be pulled in by a scope of `project.foo`.
        rows = MemoryManager.search(q='deployment', namespace_scope='project.foo',
                                    limit=50)
        self.assertNotIn('project.foo_x', self._namespaces(rows))

    def test_underscore_scope_is_literal(self):
        rows = MemoryManager.search(q='deployment', namespace_scope='project.foo_x',
                                    limit=50)
        self.assertEqual(self._namespaces(rows), ['project.foo_x'])

    def test_no_scope_is_workspace_global(self):
        # Unscoped retrieval must keep the previous behaviour exactly.
        rows = MemoryManager.search(q='deployment', limit=50)
        self.assertEqual(len(rows), 7)

    def test_unknown_scope_returns_nothing(self):
        rows = MemoryManager.search(q='deployment', namespace_scope='project.nope',
                                    limit=50)
        self.assertEqual(rows, [])


class TopForPromptScopeTests(ScopeTestCase):
    def test_scoped_injection_excludes_other_projects(self):
        rows = MemoryManager.top_for_prompt('deployment pipeline',
                                            namespace_scope='project.foo')
        got = self._namespaces(rows)
        self.assertTrue(got)
        for ns in got:
            self.assertIn(ns, self.IN_SCOPE)

    def test_stopword_only_prompt_still_honours_scope(self):
        # The empty-terms fallback path selects preference/procedural rows
        # directly; it must be scoped too, or a scoped chat with a trivial
        # prompt would still pull the whole workspace.
        MemoryManager.upsert(namespace='project.foo', key='pref',
                             value='prefers tabs', kind='preference')
        MemoryManager.upsert(namespace='project.bar', key='pref',
                             value='prefers spaces', kind='preference')
        rows = MemoryManager.top_for_prompt('the and of',
                                            namespace_scope='project.foo')
        for ns in self._namespaces(rows):
            self.assertTrue(ns.startswith('project.foo'))

    def test_unscoped_is_unchanged(self):
        rows = MemoryManager.top_for_prompt('deployment pipeline')
        self.assertTrue(len(rows) > 0)


class ListScopeTests(ScopeTestCase):
    def test_list_honours_scope(self):
        rows = MemoryManager.list(namespace_scope='project.foo', limit=50)
        self.assertEqual(self._namespaces(rows), self.IN_SCOPE)

    def test_list_with_query_honours_scope(self):
        rows = MemoryManager.list(q='deployment', namespace_scope='project.foo',
                                  limit=50)
        self.assertEqual(self._namespaces(rows), self.IN_SCOPE)


if __name__ == '__main__':
    unittest.main()
