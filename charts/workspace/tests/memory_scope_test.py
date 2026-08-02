"""Namespace-scoped memory retrieval (#359, widened in #593).

Injection used to retrieve from the whole workspace DB, so a `project.foo`
memory could surface inside a `project.bar` chat whenever it out-scored the
local ones. These tests pin the scoping contract, including the prefix-anchoring
and LIKE-escaping edge cases that make `project.foo` NOT match `project.foobar`
or `project.foo_x`.

They also pin the #593 widening: a scope is a LIST of roots, and `user.*` is
always one of them, so filing a chat into a project no longer costs it the
user's own name, preferences and working style. Every retrieval path is covered
here — FTS, the LIKE degradation, the vector-only fetch, list, and
top_for_prompt's stopword-only fallback — because a partial fix would look
correct on the obvious one.

Run with:   python3 -m unittest tests.memory_scope_test   (from charts/workspace/)
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from memory import manager as _mgr_mod  # noqa: E402
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

    # The caller's own scope…
    IN_SCOPE = ['project.foo', 'project.foo.decisions', 'project.foo.goals']
    # …and the root every scoped read also gets (#593). The fixture's `user`
    # namespace is bare, which is exactly the case the root has to cover: the
    # clause matches the root itself, not only things nested under it.
    ALWAYS = ['user']
    IN_SCOPE_593 = sorted(IN_SCOPE + ALWAYS)


class SearchScopeTests(ScopeTestCase):
    def test_scope_matches_root_and_descendants_and_user(self):
        rows = MemoryManager.search(q='deployment', namespace_scope='project.foo',
                                    limit=50)
        self.assertEqual(self._namespaces(rows), self.IN_SCOPE_593)

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
        self.assertEqual(self._namespaces(rows), ['project.foo_x', 'user'])

    def test_no_scope_is_workspace_global(self):
        # Unscoped retrieval must keep the previous behaviour exactly.
        rows = MemoryManager.search(q='deployment', limit=50)
        self.assertEqual(len(rows), 7)

    def test_unknown_scope_returns_only_the_always_included_root(self):
        # Pre-#593 this was []. A scope nothing was ever written under still
        # matches nothing OF ITS OWN — but the user's own memories are not
        # "its own", they are true everywhere.
        rows = MemoryManager.search(q='deployment', namespace_scope='project.nope',
                                    limit=50)
        self.assertEqual(self._namespaces(rows), ['user'])


class AlwaysInScopeUserRootTests(ScopeTestCase):
    """#593: `user.*` rides along on every scoped read, on every path.

    Single-rooted scoping meant a chat filed into a project could no longer
    recall the user's name or preferences. Nothing errored — it was just
    quietly less informed, which is why each retrieval path is asserted
    separately here rather than trusting the one that is easy to see.
    """

    def test_user_descendants_are_in_scope_too(self):
        MemoryManager.upsert(namespace='user.preferences', key='editor',
                             value='deployment via vim, apparently')
        rows = MemoryManager.search(q='deployment', namespace_scope='project.foo',
                                    limit=50)
        self.assertIn('user.preferences', self._namespaces(rows))

    def test_sibling_project_is_still_out_of_scope(self):
        # The widening must not become "retrieve everything": #359's guarantee
        # is what makes project scoping worth having.
        rows = MemoryManager.search(q='deployment', namespace_scope='project.foo',
                                    limit=50)
        self.assertNotIn('project.bar', self._namespaces(rows))

    def test_like_degradation_path_widens_too(self):
        # A malformed FTS query drops search to the LIKE fallback. It builds its
        # own WHERE clause, so it is a path a partial fix would miss.
        with mock.patch.object(_mgr_mod, '_build_fts_query', return_value='AND'):
            rows = MemoryManager.search(q='deployment',
                                        namespace_scope='project.foo', limit=50)
        got = self._namespaces(rows)
        self.assertIn('user', got)
        self.assertNotIn('project.bar', got)

    def test_vector_only_hits_widen_too(self):
        # _vector_search does an UNFILTERED KNN; _fetch_by_ids is the gate that
        # decides what a vector-only hit may bring back. It must apply the same
        # widened scope, or a `user.*` hit found only by embedding is dropped.
        user_row = MemoryManager.get(namespace='user', key='g')
        bar_row = MemoryManager.get(namespace='project.bar', key='f')
        with mock.patch.object(MemoryManager, '_vector_search',
                               return_value=[user_row['id'], bar_row['id']]):
            rows = MemoryManager.search(q='unrelatedterm',
                                        namespace_scope='project.foo', limit=50)
        got = self._namespaces(rows)
        self.assertIn('user', got)
        self.assertNotIn('project.bar', got)

    def test_a_user_scope_is_not_widened_back_out(self):
        # Scoping deliberately NARROW must stay narrow: `user.preferences` is
        # already inside the always-included root, so adding it would widen the
        # caller's request instead of honouring it.
        MemoryManager.upsert(namespace='user.preferences', key='editor',
                             value='deployment via vim, apparently')
        rows = MemoryManager.search(q='deployment',
                                    namespace_scope='user.preferences', limit=50)
        self.assertEqual(self._namespaces(rows), ['user.preferences'])

    def test_scoping_to_user_itself_is_not_duplicated(self):
        rows = MemoryManager.search(q='deployment', namespace_scope='user',
                                    limit=50)
        self.assertEqual(self._namespaces(rows), ['user'])

    def test_unscoped_retrieval_is_untouched(self):
        rows = MemoryManager.search(q='deployment', limit=50)
        self.assertEqual(len(rows), 7)


class ScopeRankingTests(ScopeTestCase):
    """Being in scope is not enough: `user.*` now competes for the same handful
    of injection slots, so the chat's OWN project has to win where the ranking
    has nothing else to separate them (#593)."""

    def _pin_updated_at(self, value=1000.0):
        """Flatten recency so scores are decided by the inputs under test."""
        with MemoryManager.store().conn() as c:
            c.execute('UPDATE memories SET updated_at=?, created_at=?', (value, value))

    def test_the_own_scope_wins_a_tie(self):
        # Exercised on the LIKE degradation, where every row carries fts_rank 0
        # and equal-score ties are therefore the norm rather than a coincidence.
        # Inserted user-first, so row order alone would put `user` on top: only
        # a deliberate tiebreak flips it.
        MemoryManager.upsert(namespace='user', key='tie',
                             value='identical deployment text', importance=0.5)
        MemoryManager.upsert(namespace='project.foo', key='tie',
                             value='identical deployment text', importance=0.5)
        self._pin_updated_at()
        with mock.patch.object(_mgr_mod, '_build_fts_query', return_value='AND'):
            rows = MemoryManager.search(q='identical deployment text',
                                        namespace_scope='project.foo', limit=50)
        self.assertEqual([r['namespace'] for r in rows],
                         ['project.foo', 'user'])
        self.assertEqual(rows[0]['_score'], rows[1]['_score'])  # a real tie

    def test_a_more_relevant_user_memory_still_wins(self):
        # The tiebreak is a TIEBREAK, not a priority order. "What did I say my
        # name was" inside a project chat is the whole point of #593, and it
        # only works if relevance still leads.
        MemoryManager.upsert(namespace='user', key='name',
                             value='the user is called Imran')
        rows = MemoryManager.search(q='called Imran',
                                    namespace_scope='project.foo', limit=50)
        self.assertEqual(rows[0]['namespace'], 'user')


class TopForPromptScopeTests(ScopeTestCase):
    def test_scoped_injection_excludes_other_projects(self):
        rows = MemoryManager.top_for_prompt('deployment pipeline',
                                            namespace_scope='project.foo')
        got = self._namespaces(rows)
        self.assertTrue(got)
        for ns in got:
            self.assertIn(ns, self.IN_SCOPE_593)

    def test_scoped_injection_includes_user_memories(self):
        rows = MemoryManager.top_for_prompt('deployment pipeline',
                                            namespace_scope='project.foo')
        self.assertIn('user', self._namespaces(rows))

    def test_stopword_only_prompt_still_honours_scope(self):
        # The empty-terms fallback path selects preference/procedural rows
        # directly; it must be scoped too, or a scoped chat with a trivial
        # prompt would still pull the whole workspace.
        MemoryManager.upsert(namespace='project.foo', key='pref',
                             value='prefers tabs', kind='preference')
        MemoryManager.upsert(namespace='project.bar', key='pref',
                             value='prefers spaces', kind='preference')
        for ns in self._namespaces(MemoryManager.top_for_prompt(
                'the and of', namespace_scope='project.foo')):
            self.assertNotEqual(ns, 'project.bar')

    def test_stopword_only_prompt_leads_with_the_callers_own_scope(self):
        # No query terms means no relevance signal at all, so ordering by
        # importance alone would let a high-importance `user.*` preference push
        # the project's own facts out of the k slots.
        MemoryManager.upsert(namespace='user.preferences', key='editor',
                             value='prefers vim', kind='preference',
                             importance=0.9)
        MemoryManager.upsert(namespace='project.foo', key='pref',
                             value='prefers tabs', kind='preference',
                             importance=0.1)
        rows = MemoryManager.top_for_prompt('the and of',
                                            namespace_scope='project.foo')
        namespaces = [r['namespace'] for r in rows]
        self.assertEqual(namespaces[0], 'project.foo')
        self.assertIn('user.preferences', namespaces)

    def test_unscoped_is_unchanged(self):
        rows = MemoryManager.top_for_prompt('deployment pipeline')
        self.assertTrue(len(rows) > 0)


class ListScopeTests(ScopeTestCase):
    def test_list_honours_scope(self):
        rows = MemoryManager.list(namespace_scope='project.foo', limit=50)
        self.assertEqual(self._namespaces(rows), self.IN_SCOPE_593)

    def test_list_with_query_honours_scope(self):
        # This is the hook's real path: /api/memory?q=…&namespace_scope=… lands
        # on list(q=…), which delegates to search.
        rows = MemoryManager.list(q='deployment', namespace_scope='project.foo',
                                  limit=50)
        self.assertEqual(self._namespaces(rows), self.IN_SCOPE_593)

    def test_list_still_excludes_sibling_projects(self):
        rows = MemoryManager.list(namespace_scope='project.foo', limit=50)
        self.assertNotIn('project.bar', self._namespaces(rows))


if __name__ == '__main__':
    unittest.main()
