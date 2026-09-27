"""Tests for the hypervisor route table that replaced the elif branches (#100).

Structured agent-session chat: eighteen routes across three verbs, and the
second domain (after tasks) whose branches were interleaved with another's
rather than contiguous — so the same hoist-safety question applies, and is
answered by the `hoisted_over_paths` column the harness now carries.

Named `..._table_test` because `tests/hypervisor_routes_test.py` already
exists and covers different ground: the soft-delete/revive handlers' own
behaviour against a real HypervisorSession. That suite is unchanged.

Run with:
    cd charts/workspace && python3 -m unittest tests.hypervisor_routes_table_test
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402
from handlers import hypervisor  # noqa: E402
from handlers.routing import RouteTable  # noqa: E402
from tests.http_harness import (  # noqa: E402
    DomainEndpointTests, DomainRouteTests, EndpointTestCase, args_for,
)

#: Every path the collapsed table was hoisted above. On GET the three
#: collection routes sat above five `/api/gateway/*` branches while
#: `threads/{id}` and its sub-resources sat below them; on POST the eight
#: `{id}/…` routes were in the regex block at the very bottom of the chain.
_HOISTED_OVER = (
    ('GET', '/api/gateway/whatsapp/webhook'),
    ('GET', '/api/gateway/links'),
    ('GET', '/api/gateway/providers'),
    ('GET', '/api/gateway/credentials'),
    ('GET', '/api/gateway/internal/transcript'),
    ('GET', '/api/workspace/dirs'),
    ('GET', '/api/provider-keys'),
    ('GET', '/api/mcp-servers'),
    ('GET', '/api/subscriptions'),
    ('GET', '/api/webhooks'),
    ('GET', '/api/crons/c1'),
    ('GET', '/api/page-watches/p1'),
    ('GET', '/api/projects'),
    ('GET', '/api/boards'),
    ('GET', '/api/boards/b1/items'),
    ('GET', '/api/feed'),
    ('GET', '/api/desktop'),
    ('GET', '/api/memory/ns/key'),
    ('GET', '/api/skills'),
    ('GET', '/api/docs'),
    ('GET', '/api/files/list'),
    ('POST', '/api/gateway/whatsapp/webhook'),
    ('POST', '/api/gateway/link'),
    ('POST', '/api/gateway/test'),
    ('POST', '/api/gateway/internal/inbound'),
    ('POST', '/api/gateway/internal/control'),
    ('POST', '/api/provider-keys'),
    ('POST', '/api/subscriptions/claude/login/start'),
    ('POST', '/api/mcp-servers'),
    ('POST', '/api/webhooks/wh1'),
    ('POST', '/api/webhooks/wh1/test'),
    ('POST', '/api/crons/c1/suspend'),
    ('POST', '/api/triggers/cron-fire/c1'),
    ('POST', '/api/projects'),
    ('POST', '/api/boards'),
    ('POST', '/api/boards/b1/runs'),
    ('POST', '/api/devcontainer/apply'),
    ('POST', '/api/feed'),
    ('POST', '/api/feed/fd_1/read'),
    ('POST', '/api/push/register'),
    ('POST', '/api/desktop/_reorder'),
    ('POST', '/api/memory'),
    ('POST', '/api/skills/_scan'),
    ('POST', '/api/files/upload'),
    # DELETE's two branches were already adjacent; listed so a future reorder
    # is covered anyway.
    ('DELETE', '/api/provider-keys/SOME_KEY'),
    ('DELETE', '/api/mcp-servers/name'),
    ('DELETE', '/api/subscriptions/claude'),
    ('DELETE', '/api/webhooks/wh1'),
    ('DELETE', '/api/gateway/credentials'),
    ('DELETE', '/api/projects/p1'),
    ('DELETE', '/api/boards/b1'),
    ('DELETE', '/api/desktop/d1'),
    ('DELETE', '/api/memory/ns/key'),
    ('DELETE', '/api/files'),
    ('DELETE', '/api/claude/tasks/t1'),
)


class HypervisorRouteOrderTests(DomainRouteTests, unittest.TestCase):

    table = hypervisor.ROUTES
    foreign_paths = (('GET', '/api/hypervisor'),
                     ('GET', '/api/hypervisor/threads/'),
                     ('GET', '/api/hypervisor/threadsx'),
                     ('GET', '/api/hypervisor/threads/th1/activity/2'),
                     ('GET', '/api/hypervisorx'))
    oauth_samples = (('GET', '/api/hypervisor/threads/th1'),
                     ('POST', '/api/hypervisor/transcribe'),
                     ('DELETE', '/api/hypervisor/threads/th1'))
    wrong_verb_samples = (('DELETE', '/api/hypervisor/config'),
                          ('GET', '/api/hypervisor/transcribe'),
                          ('POST', '/api/hypervisor/health'),
                          ('DELETE', '/api/hypervisor/threads/th1/rename'))
    hoisted_over_paths = _HOISTED_OVER
    owned_prefixes = ('/api/hypervisor',)

    def test_the_collection_reads(self):
        for path, handler in (
                ('/api/hypervisor/config', 'handle_hypervisor_config'),
                ('/api/hypervisor/threads', 'handle_hypervisor_list_threads'),
                ('/api/hypervisor/health', 'handle_hypervisor_health')):
            self.assertEqual(self.resolve('GET', path), handler, path)

    def test_the_thread_reads(self):
        for suffix, handler in (
                ('', 'handle_hypervisor_get_thread'),
                ('/activity', 'handle_hypervisor_get_activity'),
                ('/watchers', 'handle_hypervisor_list_watchers')):
            path = '/api/hypervisor/threads/th1' + suffix
            self.assertEqual(self.resolve('GET', path), handler, path)

    def test_the_per_thread_actions(self):
        for suffix, handler in (
                ('messages', 'handle_hypervisor_send_message'),
                ('stop', 'handle_hypervisor_stop'),
                ('restore', 'handle_hypervisor_restore_thread'),
                ('watchers', 'handle_hypervisor_create_watcher'),
                ('rename', 'handle_hypervisor_rename_thread'),
                ('model', 'handle_hypervisor_set_model'),
                ('effort', 'handle_hypervisor_set_effort'),
                ('project', 'handle_hypervisor_set_project')):
            path = f'/api/hypervisor/threads/th1/{suffix}'
            self.assertEqual(self.resolve('POST', path), handler, path)

    def test_the_creates_and_deletes(self):
        self.assertEqual(self.resolve('POST', '/api/hypervisor/threads'),
                         'handle_hypervisor_create_thread')
        self.assertEqual(self.resolve('POST', '/api/hypervisor/transcribe'),
                         'handle_hypervisor_transcribe')
        self.assertEqual(
            self.resolve('DELETE', '/api/hypervisor/threads/th1'),
            'handle_hypervisor_delete_thread')
        self.assertEqual(
            self.resolve('DELETE', '/api/hypervisor/threads/th1/watchers/w1'),
            'handle_hypervisor_cancel_watcher')

    def test_the_thread_id_is_passed_as_a_capture_group(self):
        # Every handler in this domain takes its thread id as a real
        # parameter, so nothing here needs the `sets=` column.
        self.assertEqual(
            args_for(hypervisor.ROUTES, 'GET', '/api/hypervisor/threads/th1'),
            ('th1',))
        self.assertEqual(
            args_for(hypervisor.ROUTES, 'DELETE',
                     '/api/hypervisor/threads/th1/watchers/w9'),
            ('th1', 'w9'))
        self.assertEqual(set(r.sets for r in hypervisor.ROUTES.routes), {()})

    def test_watchers_is_three_routes_on_three_verbs(self):
        # GET lists, POST arms, DELETE cancels one by id — and the cancel
        # route has to stay ahead of the thread soft-delete.
        self.assertEqual(
            self.resolve('GET', '/api/hypervisor/threads/th1/watchers'),
            'handle_hypervisor_list_watchers')
        self.assertEqual(
            self.resolve('POST', '/api/hypervisor/threads/th1/watchers'),
            'handle_hypervisor_create_watcher')
        self.assertEqual(
            self.resolve('DELETE', '/api/hypervisor/threads/th1/watchers/w1'),
            'handle_hypervisor_cancel_watcher')

    def test_the_specific_routes_are_safe_by_construction_not_by_order(self):
        # A thread id is [A-Za-z0-9_-]+, which excludes '/', and every pattern
        # is anchored — so `{id}/activity` cannot be read as an `{id}` even
        # when the bare route is tried first. Resolve against a reversed copy
        # of the table and require the same answers.
        inverted = RouteTable()
        inverted.routes.extend(reversed(hypervisor.ROUTES.routes))
        for http_method, path in (
                ('GET', '/api/hypervisor/threads/th1/activity'),
                ('GET', '/api/hypervisor/threads/th1/watchers'),
                ('POST', '/api/hypervisor/threads/th1/model'),
                ('DELETE', '/api/hypervisor/threads/th1/watchers/w1')):
            self.assertEqual(inverted.match(http_method, path, path)[0].handler,
                             self.resolve(http_method, path), path)

    def test_an_unknown_action_is_not_read_as_a_thread_id(self):
        # There is no bare POST {id} route, so a typo'd action falls through
        # rather than being treated as a thread.
        self.assertIsNone(
            self.resolve('POST', '/api/hypervisor/threads/th1/nope'))


class HypervisorEndpointBehaviourTests(DomainEndpointTests, EndpointTestCase):
    """Status codes for the migrated routes, over a real server."""

    oauth_reachable = (('GET', '/api/hypervisor/threads'),
                       ('POST', '/api/hypervisor/transcribe'),
                       ('DELETE', '/api/hypervisor/threads/th1'))
    unmatched_shapes = (('GET', '/api/hypervisor/threads/'),
                        ('GET', '/api/hypervisor/threads/th1/activity/2'),
                        ('POST', '/api/hypervisor/threads/th1/nope'))

    def test_the_reads_are_auth_gated(self):
        # 401 rather than 404 is also the proof the route matched at all.
        for path in ('/api/hypervisor/config', '/api/hypervisor/threads',
                     '/api/hypervisor/health', '/api/hypervisor/threads/th1',
                     '/api/hypervisor/threads/th1/activity',
                     '/api/hypervisor/threads/th1/watchers'):
            self.assertEqual(self.get(path)[0], 401, path)

    def test_the_writes_are_auth_gated(self):
        # No bare POST {id} route exists — only the collection and the eight
        # named actions, which is why the loop starts at /messages.
        for suffix in ('/messages', '/stop', '/restore', '/watchers',
                       '/rename', '/model', '/effort', '/project'):
            path = '/api/hypervisor/threads/th1' + suffix
            self.assertEqual(self.post(path)[0], 401, path)
        for path in ('/api/hypervisor/threads', '/api/hypervisor/transcribe'):
            self.assertEqual(self.post(path)[0], 401, path)

    def test_the_deletes_are_auth_gated(self):
        for path in ('/api/hypervisor/threads/th1',
                     '/api/hypervisor/threads/th1/watchers/w1'):
            self.assertEqual(self.request(path, method='DELETE')[0], 401, path)


if __name__ == '__main__':
    unittest.main()
