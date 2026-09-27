"""Tests for the tasks route table that replaced the elif branches (#100).

The Claude Task API and the isolated worktrees behind it — twenty-two routes
across three verbs, and the first domain in the series whose branches were
**not contiguous**. Four layers:

  * The table — every route resolves to its own handler, and the `{id}/…`
    family is shown safe by construction rather than by order.
  * **Hoist safety** — the one thing this domain needs that the earlier ones
    did not. Collapsing interleaved branches moved the later task routes above
    the hypervisor, gateway and missioncontrol ones they used to sit under, so
    the paths they jumped over are listed here by name and asserted not to
    match anything in this table.
  * `sets=` and the capture groups, including the two that stay positional.
  * End-to-end over a real server.

Handler behaviour is covered by tests/server_test.py,
tests/task_worktree_test.py, tests/scroll_mode_test.py and
tests/send_key_test.py, all unchanged.

Run with:
    cd charts/workspace && python3 -m unittest tests.tasks_routes_test
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402
from handlers import tasks  # noqa: E402
from handlers.routing import RouteTable  # noqa: E402
from tests.http_harness import (  # noqa: E402
    DomainEndpointTests, DomainRouteTests, EndpointTestCase, args_for,
    dispatch_to_mock, handler_for)


class TaskRouteResolutionTests(DomainRouteTests, unittest.TestCase):

    table = tasks.ROUTES
    foreign_paths = (('GET', '/api/claude'), ('GET', '/api/claude/tasks/'),
                     ('GET', '/api/claude/tasksx'),
                     ('GET', '/api/claude/tasks/t1/output/2'),
                     ('GET', '/api/worktreesx'))
    oauth_samples = (('GET', '/api/claude/tasks/t1'),
                     ('POST', '/api/worktrees/sweep'),
                     ('DELETE', '/api/claude/tasks/t1'))
    wrong_verb_samples = (('DELETE', '/api/claude/tasks'),
                          ('GET', '/api/worktrees/sweep'),
                          ('POST', '/api/claude/tasks/t1/output'),
                          ('GET', '/api/worktrees/repo/slug'))
    strip_query_handlers = {'handle_task_worktree_remove',
                            'handle_worktrees_remove'}

    def test_the_reads(self):
        for path, expected in (
                ('/api/claude/tasks', 'handle_claude_list_tasks'),
                ('/api/claude/auth/token', 'handle_claude_get_token'),
                ('/api/claude/assistants', 'handle_claude_list_assistants'),
                ('/api/claude/tasks/t1', 'handle_claude_get_task'),
                ('/api/claude/tasks/t1/output', 'handle_claude_get_output'),
                ('/api/claude/tasks/t1/stream', 'handle_claude_stream_output'),
                ('/api/worktrees', 'handle_worktrees_list')):
            self.assertEqual(self.resolve('GET', path), expected, path)

    def test_the_writes(self):
        for path, expected in (
                ('/api/claude/tasks', 'handle_claude_create_task'),
                ('/api/claude/tasks/terminal',
                 'handle_claude_create_terminal_task'),
                ('/api/worktrees/sweep', 'handle_worktrees_sweep'),
                ('/api/claude/auth/token/regenerate',
                 'handle_claude_regenerate_token'),
                ('/api/claude/tasks/t1/message', 'handle_claude_followup'),
                ('/api/claude/tasks/t1/rename', 'handle_claude_rename_task'),
                ('/api/claude/tasks/t1/redeliver-hook',
                 'handle_claude_redeliver_hook'),
                ('/api/claude/tasks/t1/prepare-terminal',
                 'handle_claude_prepare_terminal'),
                ('/api/claude/tasks/t1/scroll-mode',
                 'handle_claude_scroll_mode'),
                ('/api/claude/tasks/t1/key', 'handle_claude_send_key')):
            self.assertEqual(self.resolve('POST', path), expected, path)

    def test_the_deletes(self):
        for path, expected in (
                ('/api/claude/tasks/t1', 'handle_claude_delete_task'),
                ('/api/claude/tasks/t1/worktree',
                 'handle_task_worktree_remove'),
                ('/api/worktrees/repo/slug', 'handle_worktrees_remove')):
            self.assertEqual(self.resolve('DELETE', path), expected, path)

    def test_the_worktree_route_split_on_the_optional_group(self):
        # Was one pattern, `/worktree(/diff)?`, whose handler branched on
        # whether group 2 matched. Two anchored routes, same resolution.
        self.assertEqual(self.resolve('GET', '/api/claude/tasks/t1/worktree'),
                         'handle_task_worktree_status')
        self.assertEqual(
            self.resolve('GET', '/api/claude/tasks/t1/worktree/diff'),
            'handle_task_worktree_diff')
        # Nothing else under /worktree/ resolves, as before.
        self.assertIsNone(
            self.resolve('GET', '/api/claude/tasks/t1/worktree/other'))

    def test_terminal_is_a_route_not_a_task_id(self):
        # POST /api/claude/tasks/terminal creates a plain-bash task. There is
        # no bare POST {id} route for it to be read as.
        self.assertEqual(self.resolve('POST', '/api/claude/tasks/terminal'),
                         'handle_claude_create_terminal_task')
        # On GET there never was a /terminal route, so "terminal" IS read as a
        # task id. Preserved, not fixed.
        self.assertEqual(self.resolve('GET', '/api/claude/tasks/terminal'),
                         'handle_claude_get_task')

    def test_the_specific_routes_are_safe_by_construction_not_by_order(self):
        # A task id is [A-Za-z0-9_-]+, which excludes '/', and every pattern is
        # anchored — so a `{id}/output` path cannot be read as an `{id}` even
        # when the bare route is tried first. Resolve against a reversed copy
        # of the table and require the same answers.
        inverted = RouteTable()
        inverted.routes.extend(reversed(tasks.ROUTES.routes))
        for http_method, path in (
                ('GET', '/api/claude/tasks/t1/output'),
                ('GET', '/api/claude/tasks/t1/stream'),
                ('GET', '/api/claude/tasks/t1/worktree'),
                ('GET', '/api/claude/tasks/t1/worktree/diff'),
                ('POST', '/api/claude/tasks/terminal'),
                ('POST', '/api/claude/tasks/t1/message'),
                ('DELETE', '/api/claude/tasks/t1/worktree')):
            self.assertEqual(inverted.match(http_method, path, path)[0].handler,
                             self.resolve(http_method, path), path)


#: Paths owned by routes that the task table was hoisted above. On GET the task
#: branches straddled five hypervisor, five gateway, two missioncontrol and two
#: other routes; on POST the six `{id}/…` ones sat below the whole rest of the
#: chain. Collapsing the domain into one table moved the later task routes over
#: all of these, which is behaviour-preserving only while none of them can be
#: matched by anything in the table.
_JUMPED_OVER = {
    'GET': (
        '/api/missioncontrol/queue',
        '/api/missioncontrol/cards/build:b1',
        '/api/claude/apps/session',
        '/api/hypervisor/config',
        '/api/hypervisor/threads',
        '/api/hypervisor/health',
        '/api/hypervisor/threads/th1',
        '/api/hypervisor/threads/th1/activity',
        '/api/hypervisor/threads/th1/watchers',
        '/api/workspace/dirs',
        '/api/gateway/whatsapp/webhook',
        '/api/gateway/links',
        '/api/gateway/providers',
        '/api/gateway/credentials',
        '/api/gateway/internal/transcript',
        '/api/provider-keys',
        '/api/mcp-servers',
        '/api/subscriptions',
        '/api/webhooks',
        '/api/crons/c1',
        '/api/page-watches/p1',
        '/api/projects',
        '/api/projects/p1/brief',
        '/api/boards',
        '/api/boards/b1/items',
        '/api/feed',
        '/api/desktop',
        '/api/memory/ns/key',
        '/api/skills',
        '/api/docs',
        '/api/files/list',
    ),
    'POST': (
        '/api/hypervisor/threads',
        '/api/hypervisor/transcribe',
        '/api/hypervisor/threads/th1/messages',
        '/api/hypervisor/threads/th1/stop',
        '/api/hypervisor/threads/th1/restore',
        '/api/hypervisor/threads/th1/watchers',
        '/api/hypervisor/threads/th1/rename',
        '/api/hypervisor/threads/th1/model',
        '/api/hypervisor/threads/th1/effort',
        '/api/hypervisor/threads/th1/project',
        '/api/gateway/whatsapp/webhook',
        '/api/gateway/link',
        '/api/gateway/test',
        '/api/gateway/internal/inbound',
        '/api/gateway/internal/control',
        '/api/provider-keys',
        '/api/subscriptions/claude/login/start',
        '/api/mcp-servers',
        '/api/webhooks/wh1',
        '/api/webhooks/wh1/test',
        '/api/crons/c1/suspend',
        '/api/triggers/cron-fire/c1',
        '/api/projects',
        '/api/projects/_discover',
        '/api/boards',
        '/api/boards/b1/runs',
        '/api/devcontainer/apply',
        '/api/feed',
        '/api/feed/fd_1/read',
        '/api/push/register',
        '/api/desktop/_reorder',
        '/api/memory',
        '/api/skills/_scan',
        '/api/files/upload',
    ),
    # DELETE's three task branches were already contiguous; nothing was
    # hoisted. Listed anyway so a future edit that reorders them is covered.
    'DELETE': (
        '/api/hypervisor/threads/th1',
        '/api/hypervisor/threads/th1/watchers/w1',
        '/api/provider-keys/SOME_KEY',
        '/api/mcp-servers/name',
        '/api/subscriptions/claude',
        '/api/webhooks/wh1',
        '/api/gateway/credentials',
        '/api/projects/p1',
        '/api/boards/b1',
        '/api/desktop/d1',
        '/api/memory/ns/key',
        '/api/files',
    ),
}


class TaskHoistSafetyTests(unittest.TestCase):
    """The proof that collapsing non-contiguous branches changed nothing.

    Every domain lifted before this one occupied an unbroken run of elif
    branches, so the old order was reproduced by copying. This one was
    interleaved, so the argument has to be made explicitly — and checked, so
    that a route added to this table later cannot quietly start shadowing one
    of the endpoints it now sits above.
    """

    def test_no_task_route_matches_a_path_it_was_hoisted_above(self):
        for http_method, paths in _JUMPED_OVER.items():
            for path in paths:
                self.assertIsNone(
                    handler_for(tasks.ROUTES, http_method, path),
                    f'{http_method} {path} is now shadowed by the task table')

    def test_the_jumped_over_prefixes_are_disjoint_from_this_domain(self):
        # The structural reason the check above passes: every path this table
        # owns starts with one of four literals, and nothing hoisted over does.
        owned = ('/api/claude/tasks', '/api/claude/auth', '/api/claude/assistants',
                 '/api/worktrees')
        for paths in _JUMPED_OVER.values():
            for path in paths:
                self.assertFalse(
                    any(path.startswith(p) for p in owned),
                    f'{path} shares a prefix with this domain')


class TaskCaptureArgumentTests(unittest.TestCase):
    """How capture groups reach the handlers: `sets=` for most, positionally
    for the two that take real parameters."""

    def test_the_task_id_lands_on_the_attribute_each_handler_reads(self):
        for http_method, path in (('GET', '/api/claude/tasks/t1'),
                                  ('GET', '/api/claude/tasks/t1/output'),
                                  ('POST', '/api/claude/tasks/t1/key'),
                                  ('DELETE', '/api/claude/tasks/t1')):
            h = dispatch_to_mock(tasks.ROUTES, http_method, path)
            self.assertEqual(h._claude_task_id, 't1', path)

    def test_worktrees_remove_still_takes_its_two_groups_positionally(self):
        # The only route in this domain whose handler declares parameters.
        h = dispatch_to_mock(tasks.ROUTES, 'DELETE', '/api/worktrees/myrepo/my-slug')
        h.handle_worktrees_remove.assert_called_once_with('myrepo', 'my-slug')



class TaskQueryStringTests(unittest.TestCase):
    """`strip_query=`: DELETE routes on the un-stripped path, and both worktree
    deletes take their options from the query string. (Which routes carry the
    column is asserted generically — see `strip_query_handlers` above.)"""

    def _handler_for(self, http_method, path):
        return handler_for(tasks.ROUTES, http_method, path)

    def test_the_worktree_deletes_match_with_a_query_string_attached(self):
        for path, expected in (
                ('/api/claude/tasks/t1/worktree?force=1',
                 'handle_task_worktree_remove'),
                ('/api/worktrees/repo/slug?force=1', 'handle_worktrees_remove')):
            self.assertEqual(self._handler_for('DELETE', path), expected, path)

    def test_the_query_string_is_not_captured_as_part_of_a_group(self):
        self.assertEqual(
            args_for(tasks.ROUTES, 'DELETE', '/api/worktrees/repo/slug?force=1'),
            ('repo', 'slug'))

    def test_a_route_without_strip_query_does_not_match_one(self):
        # Preserved as found: only the two worktree deletes split the query
        # off. The task delete never did, and do_GET strips it up front.
        self.assertIsNone(self._handler_for('DELETE', '/api/claude/tasks/t1?x=1'))


class TaskEndpointBehaviourTests(DomainEndpointTests, EndpointTestCase):
    """Status codes for the migrated routes, over a real server."""

    oauth_reachable = (('GET', '/api/claude/tasks'),
                       ('POST', '/api/worktrees/sweep'),
                       ('DELETE', '/api/claude/tasks/t1/worktree?force=1'))
    unmatched_shapes = (('GET', '/api/claude/tasks/t1/nope'),
                        ('GET', '/api/worktrees/only-one-segment'))

    def test_the_reads_are_auth_gated(self):
        # 401 rather than 404 is also the proof the route matched at all.
        for path in ('/api/claude/tasks', '/api/claude/tasks/t1',
                     '/api/claude/tasks/t1/output',
                     '/api/claude/tasks/t1/worktree',
                     '/api/claude/tasks/t1/worktree/diff',
                     '/api/claude/assistants', '/api/worktrees'):
            self.assertEqual(self.get(path)[0], 401, path)

    def test_the_writes_are_auth_gated(self):
        for path in ('/api/claude/tasks', '/api/claude/tasks/terminal',
                     '/api/worktrees/sweep',
                     '/api/claude/tasks/t1/message',
                     '/api/claude/tasks/t1/rename',
                     '/api/claude/tasks/t1/redeliver-hook',
                     '/api/claude/tasks/t1/prepare-terminal',
                     '/api/claude/tasks/t1/scroll-mode',
                     '/api/claude/tasks/t1/key'):
            self.assertEqual(self.post(path)[0], 401, path)

    def test_the_deletes_are_auth_gated(self):
        for path in ('/api/claude/tasks/t1', '/api/claude/tasks/t1/worktree',
                     '/api/claude/tasks/t1/worktree?force=1',
                     '/api/worktrees/repo/slug'):
            self.assertEqual(
                self.request(path, method='DELETE')[0], 401, path)

    def test_the_token_endpoints_are_oauth_only(self):
        # Stricter than the rest: minting or rotating the bearer token from a
        # bearer-authed call would let a leaked token renew itself.
        self.assertEqual(self.get('/api/claude/auth/token')[0], 401)
        self.assertEqual(self.post('/api/claude/auth/token/regenerate')[0], 401)



if __name__ == '__main__':
    unittest.main()
